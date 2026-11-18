"""Small Blender helpers shared by the Family Radio enclosure concepts.

All units are millimetres. Solids are built from primitives, rounded with bevel
modifiers, and combined with exact booleans so exports are closed meshes.
"""
import math
import os

import bmesh
import bpy
from mathutils import Vector


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    s = bpy.context.scene
    s.unit_settings.system = "METRIC"
    s.unit_settings.scale_length = 0.001
    s.unit_settings.length_unit = "MILLIMETERS"


def _new(name):
    o = bpy.context.object
    o.name = name
    return o


def _apply(o):
    bpy.context.view_layer.objects.active = o
    for m in list(o.modifiers):
        bpy.ops.object.modifier_apply(modifier=m.name)


def box(size, at=(0, 0, 0), r=0.0, seg=6, name="box", vertical_only=False):
    """Axis-aligned box centred at `at`, optionally with rounded edges of radius r."""
    bpy.ops.mesh.primitive_cube_add(size=1, location=at)
    o = _new(name)
    o.scale = size
    bpy.ops.object.transform_apply(scale=True)
    if r > 0:
        if vertical_only:
            # Round only the four vertical edges (a rounded-rectangle prism).
            o.data.edges.foreach_set("select", [False] * len(o.data.edges))
            for e in o.data.edges:
                a, b = (o.data.vertices[i].co for i in e.vertices)
                e.select = abs(a.x - b.x) < 1e-6 and abs(a.y - b.y) < 1e-6
            bw = o.data.attributes.new("bevel_weight_edge", "FLOAT", "EDGE")
            for e in o.data.edges:
                bw.data[e.index].value = 1.0 if e.select else 0.0
        m = o.modifiers.new("bevel", "BEVEL")
        m.width = r
        m.segments = seg
        m.limit_method = "WEIGHT" if vertical_only else "NONE"
        m.profile = 0.5
        _apply(o)
    return o


def cyl(r, h, at=(0, 0, 0), axis="Z", n=96, name="cyl", r2=None):
    """Cylinder (or cone when r2 is given) of height h centred at `at` along an axis."""
    if r2 is None:
        bpy.ops.mesh.primitive_cylinder_add(radius=r, depth=h, vertices=n, location=at)
    else:
        bpy.ops.mesh.primitive_cone_add(radius1=r, radius2=r2, depth=h, vertices=n, location=at)
    o = _new(name)
    if axis == "X":
        o.rotation_euler = (0, math.pi / 2, 0)
    elif axis == "Y":
        o.rotation_euler = (-math.pi / 2, 0, 0)
    bpy.ops.object.transform_apply(rotation=True)
    return o


def round_edges(o, r, seg=6, angle=30):
    """Fillet every sharp edge of an already-built solid (use on simple shapes only)."""
    m = o.modifiers.new("bevel", "BEVEL")
    m.width = r
    m.segments = seg
    m.limit_method = "ANGLE"
    m.angle_limit = math.radians(angle)
    m.harden_normals = False
    _apply(o)
    return o


def _boolean(o, others, op):
    for x in others:
        m = o.modifiers.new("bool", "BOOLEAN")
        m.object = x
        m.operation = op
        m.solver = "EXACT"
        _apply(o)
        bpy.data.objects.remove(x, do_unlink=True)
    return clean(o)


def clean(o):
    """Merge coincident vertices and dissolve zero-area slivers left by booleans/bevels."""
    bm = bmesh.new()
    bm.from_mesh(o.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-4)
    bmesh.ops.dissolve_degenerate(bm, edges=bm.edges, dist=1e-4)
    bm.to_mesh(o.data)
    bm.free()
    return o


def cut(o, *cutters):
    return _boolean(o, cutters, "DIFFERENCE")


def add(o, *others):
    return _boolean(o, others, "UNION")


def keep(o, *others):
    return _boolean(o, others, "INTERSECT")


def move(o, d):
    o.location = Vector(o.location) + Vector(d)
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.ops.object.transform_apply(location=True)
    return o


def rotate(o, deg, axis="X", pivot=(0, 0, 0)):
    """Rotate a mesh about an axis through `pivot` and bake the transform."""
    move(o, [-p for p in pivot])
    o.rotation_euler = [math.radians(deg) if a == axis else 0 for a in "XYZ"]
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.ops.object.transform_apply(rotation=True)
    return move(o, pivot)


def dup(o, name):
    c = o.copy()
    c.data = o.data.copy()
    c.name = name
    bpy.context.collection.objects.link(c)
    return c


def bounds(o):
    xs = [v.co for v in o.data.vertices]
    return (Vector([min(v[i] for v in xs) for i in range(3)]),
            Vector([max(v[i] for v in xs) for i in range(3)]))


def export_stl(o, path, print_rot=None):
    """Export a copy of `o` in print orientation, resting on z=0 and centred on the bed.

    print_rot is an optional (degrees, axis) rotation applied to the copy only, so
    the assembled model stays in place for rendering.
    """
    c = dup(o, o.name + "_print")
    if print_rot:
        rotate(c, *print_rot)
    lo, hi = bounds(c)
    move(c, (-(lo.x + hi.x) / 2, -(lo.y + hi.y) / 2, -lo.z))
    bpy.ops.object.select_all(action="DESELECT")
    c.select_set(True)
    bpy.context.view_layer.objects.active = c
    os.makedirs(os.path.dirname(path), exist_ok=True)
    bpy.ops.wm.stl_export(filepath=path, export_selected_objects=True, ascii_format=False,
                          apply_modifiers=True, global_scale=1.0)
    bpy.data.objects.remove(c, do_unlink=True)


def material(o, rgb, rough=0.55, metal=0.0, emit=None):
    m = bpy.data.materials.new(o.name + "_mat")
    m.use_nodes = True
    p = m.node_tree.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = (*rgb, 1)
    p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = metal
    if emit:
        p.inputs["Emission Color"].default_value = (*emit, 1)
        p.inputs["Emission Strength"].default_value = 1.5
    o.data.materials.clear()
    o.data.materials.append(m)
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.shade_smooth_by_angle(angle=math.radians(35))
    return o


def render(path, objs, cam_dir=(1.0, -1.6, 0.9), lens=60, res=(1400, 1050), samples=48):
    """Studio render of `objs` (already placed) to a JPEG with Cycles on the CPU."""
    s = bpy.context.scene
    for o in bpy.context.scene.objects:
        o.hide_render = o not in objs
    lo = Vector([min(bounds(o)[0][i] for o in objs) for i in range(3)])
    hi = Vector([max(bounds(o)[1][i] for o in objs) for i in range(3)])
    centre, radius = (lo + hi) / 2, (hi - lo).length / 2

    floor_name = "_floor"
    if floor_name not in bpy.data.objects:
        bpy.ops.mesh.primitive_plane_add(size=4000)
        f = _new(floor_name)
        material(f, (0.93, 0.92, 0.9), rough=0.9)
        for name, loc, power, size in (("_key", (-1, -1, 1.4), 5e6, 900),
                                       ("_fill", (1.3, -0.4, 0.6), 1.6e6, 1200),
                                       ("_rim", (0.2, 1.4, 1.1), 2e6, 700)):
            bpy.ops.object.light_add(type="AREA")
            L = _new(name)
            L.data.energy, L.data.size = power, size
        bpy.ops.object.camera_add()
        s.camera = _new("_cam")
        w = bpy.data.worlds.new("w")
        w.use_nodes = True
        w.node_tree.nodes["Background"].inputs[0].default_value = (0.9, 0.9, 0.9, 1)
        w.node_tree.nodes["Background"].inputs[1].default_value = 0.3
        s.world = w
    f = bpy.data.objects[floor_name]
    f.location.z = lo.z
    f.hide_render = False
    for name, loc in (("_key", (-1, -1, 1.4)), ("_fill", (1.3, -0.4, 0.6)), ("_rim", (0.2, 1.4, 1.1))):
        L = bpy.data.objects[name]
        L.location = centre + Vector(loc) * 900
        L.rotation_euler = (centre - L.location).to_track_quat("-Z", "Y").to_euler()
        L.hide_render = False
    cam = bpy.data.objects["_cam"]
    cam.data.lens = lens
    cam.data.clip_end = 10000
    d = Vector(cam_dir).normalized()
    cam.location = centre + d * radius * (lens / 13.0)
    cam.rotation_euler = (centre - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam.hide_render = False

    s.render.engine = "CYCLES"
    s.cycles.device = "CPU"
    s.cycles.samples = samples
    s.cycles.use_denoising = True
    s.render.resolution_x, s.render.resolution_y = res
    s.render.film_transparent = False
    s.view_settings.view_transform = "AgX"
    s.view_settings.look = "AgX - Medium High Contrast"
    s.render.image_settings.file_format = "JPEG"   # smooth renders: ~10x smaller than PNG
    s.render.image_settings.quality = 90
    # Blender can embed stamp fields even when the visible stamp is disabled.
    s.render.use_stamp = False
    for field in s.render.bl_rna.properties:
        if field.identifier.startswith("use_stamp_"):
            setattr(s.render, field.identifier, False)
    s.render.filepath = path
    bpy.ops.render.render(write_still=True)


def render_section(path, objs, x=0.0, cam_dir=(1.0, -0.45, 0.3), **kw):
    """Render the assembly cut in half at plane X = x, viewed from the cut side."""
    halves = []
    for o in objs:
        c = dup(o, o.name + "_half")
        lo, hi = bounds(c)
        if lo.x >= x:
            bpy.data.objects.remove(c, do_unlink=True)
            continue
        if hi.x > x:
            cut(c, box((2000, 2000, 2000), (x + 1000, 0, 0)))
        if c.data.polygons:
            halves.append(c)
        else:
            bpy.data.objects.remove(c, do_unlink=True)
    render(path, halves, cam_dir=cam_dir, **kw)
    for c in halves:
        bpy.data.objects.remove(c, do_unlink=True)


def paint(printed, ref, palette, default):
    """Printed parts in their palette colours; reference electronics in greys."""
    for o in printed:
        material(o, palette.get(o.name, default))
    for o in ref:
        rgb = (0.04, 0.04, 0.05) if "screen" in o.name else (0.85, 0.85, 0.87) if o.name == "ref-dial" else (0.45, 0.47, 0.5)
        material(o, rgb, rough=0.4)


def frustum(w0, h0, w1, h1, y0, y1, z, x=0.0, name="frustum"):
    """Rectangular frustum along Y: w0 x h0 (XZ) at y0 tapering to w1 x h1 at y1 (cutter)."""
    return hull([(x + sx * w / 2, y, z + sz * h / 2) for w, h, y in ((w0, h0, y0), (w1, h1, y1))
                 for sx, sz in ((-1, -1), (1, -1), (1, 1), (-1, 1))], name)


def hull(points, name="hull"):
    """Convex solid spanning the given points."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    for p in points:
        bm.verts.new(p)
    bmesh.ops.convex_hull(bm, input=bm.verts)
    bm.to_mesh(me)
    bm.free()
    o = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(o)
    return o


def label(text, at, size=3.0, face="front", align="CENTER", rgb=(0.1, 0.1, 0.1), emit=None, name="label"):
    """Render-only flat lettering on a face, like a printed decal. Never exported.
    face "front" reads on a -Y face, "top" on a +Z face (bottom of the text toward -Y)."""
    bpy.ops.object.text_add()
    o = _new(name)
    o.data.body = text
    o.data.size = size
    o.data.align_x = align
    o.data.align_y = "CENTER"
    o.data.space_character = 1.2
    bpy.ops.object.convert(target="MESH")
    o = bpy.context.object
    o.name = name
    if face == "front":
        rotate(o, 90, "X")
    move(o, at)
    return material(o, rgb, emit=emit)


def join(objs, name):
    """Join disjoint meshes into one object without a boolean (for hole-field cutters)."""
    bpy.ops.object.select_all(action="DESELECT")
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    objs[0].name = name
    return objs[0]


def screen_art(at, w, h, face="front"):
    """Render-only playlist picture for a w x h screen centred at `at`: a ringed
    planet and a title, as a stand-in for the unbuilt UI. Never exported."""
    s, wide = min(w, h), w > h * 1.1
    px, py = (-w * 0.27, 0.0) if wide else (0.0, h * 0.14)
    planet = cyl(s * (0.15 if wide else 0.19), 0.05, (px, py, 0.05), n=64, name="ui-planet")
    k = 0.8 if wide else 1.0
    ring = cyl(s * 0.33 * k, 0.04, (0, 0, 0.04), n=64, name="ui-ring")
    cut(ring, cyl(s * 0.27 * k, 1, (0, 0, 0), n=64))
    ring.scale = (1, 0.32, 1)
    bpy.context.view_layer.objects.active = ring
    bpy.ops.object.transform_apply(scale=True)
    rotate(ring, 18, "Z")
    move(ring, (px, py, 0.02))
    material(planet, (0.98, 0.42, 0.1), emit=(0.98, 0.42, 0.1))
    material(ring, (0.95, 0.9, 0.8), emit=(0.95, 0.9, 0.8))
    tx, ty = (w * 0.22, 0.0) if wide else (0.0, -h * 0.3)
    text = label("SPACE\nSONGS", (tx, ty, 0.08), size=s * (0.12 if wide else 0.13), face="top", rgb=(0.95, 0.9, 0.8),
                 emit=(0.95, 0.9, 0.8), name="ui-title")
    art = [planet, ring, text]
    for o in art:
        if face == "front":
            rotate(o, 90, "X")
        move(o, at)
    return art


def solid(objs):
    """Drop render-only decoration (labels, screen art), e.g. before a section cut."""
    return [o for o in objs if not o.name.startswith(("label", "ui-"))]
