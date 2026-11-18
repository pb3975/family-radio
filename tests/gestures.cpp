#include "firmware/family_dial/gestures.h"
#include <cassert>
#include "firmware/family_dial/clock.h"
#include "firmware/family_dial/ui.h"
int main() {
  FaceGesture f;
  f.press(0,true); assert(!f.hold(599)); assert(f.hold(600)); assert(!f.hold(601)); assert(!f.release());
  f.press(0,true); assert(f.release()); assert(!f.release());
  f.press(0,false); assert(!f.hold(1000)); assert(!f.release());
  f.press(UINT32_MAX-300,true); assert(f.hold(300)); assert(!f.release());
  f.press(0,true); f.reset(); assert(!f.release());
  assert(skipTarget(60,190)==-1); assert(skipTarget(160,190)==1); assert(skipTarget(120,190)==0);
  assert(marqueeOffset(100,112,10000)==0);
  assert(marqueeOffset(212,112,1400)==0);
  assert(marqueeOffset(212,112,1450)==1);
  assert(marqueeOffset(212,112,6400)==100);
  assert(marqueeOffset(212,112,7799)==100);
  assert(marqueeOffset(212,112,7800)==0);
  VolumeIndicator volume;
  assert(volume.brightness(0)==0);volume.changed(100);
  assert(volume.brightness(1899)==255);assert(volume.brightness(2000)>0 && volume.brightness(2000)<255);
  assert(volume.brightness(2400)==0);assert(!volume.active);
  assert(volume.brightness(100)==0); // An expired indicator cannot reappear after a full clock wrap.
  volume.changed(UINT32_MAX-99);assert(volume.brightness(0)==255);assert(volume.brightness(2200)==0);
  volume.reset();assert(volume.brightness(0)==0);
  IdleClock clock;
  clock.tick(0,true);clock.tick(119999,true);assert(!clock.visible);
  clock.tick(120000,true);assert(clock.visible);
  assert(clock.wake(120001,true,true));assert(!clock.visible);
  assert(clock.wake(121000,true,true));assert(clock.wake(121050,true,false));
  assert(clock.wake(121100,false,false));assert(clock.wake(121400,false,false));assert(!clock.wake(121401,true,false));
  clock.tick(400000,false);clock.tick(500000,true);assert(!clock.visible);
  clock.tick(619999,true);assert(!clock.visible);clock.tick(620000,true);assert(clock.visible);
  clock.tick(620001,false);assert(!clock.visible);
  clock.reset(UINT32_MAX-60000);clock.tick(UINT32_MAX-60000,true);clock.tick(59999,true);assert(clock.visible);
  struct tm date;assert(!clockDate(0,date));assert(clockDate(29912328,date));
  assert(clockStyle(0)==ClockStyle::Quiet&&clockStyle(1)==ClockStyle::Analog&&clockStyle(2)==ClockStyle::Color&&clockStyle(255)==ClockStyle::Color);
  LocalUI ui;
  assert(ui.clock==ClockStyle::Color);
  ui.page=Page::Settings;ui.selected=1;ui.choose();assert(ui.page==Page::Clock);
  ui.choose();assert(ui.clock==ClockStyle::Quiet&&ui.page==Page::Settings);
  ui.page=Page::Clock;ui.selected=1;ui.choose();assert(ui.clock==ClockStyle::Analog);
  ui.page=Page::Clock;ui.selected=2;ui.choose();assert(ui.clock==ClockStyle::Color);
  ui.page=Page::Clock;ui.selected=3;ui.choose();assert(ui.clock==ClockStyle::Color&&ui.page==Page::Settings);
  ui.reset();
  assert(!ui.touch(60,190));assert(!ui.open());
  assert(ui.touch(120,230));assert(ui.page==Page::Menu);
  ui.turn(100);assert(ui.selected==6);ui.turn(-100);assert(ui.selected==0);
  ui.turn(4);ui.choose();assert(ui.page==Page::Display);
  ui.turn(1);ui.choose();assert(!ui.open()&&ui.mode==VisualMode::Colorful);
  ui.menu();ui.turn(5);ui.choose();assert(ui.page==Page::Settings);
  ui.turn(2);assert(ui.selected==2);ui.choose();assert(ui.page==Page::Menu);
  ui.menu();ui.turn(5);ui.choose();assert(ui.page==Page::Settings);
  ui.choose();assert(ui.page==Page::WifiConfirm && ui.selected==0);
  ui.choose();assert(ui.page==Page::Settings);
  ui.reset();assert(!ui.open());assert(ui.mode==VisualMode::Colorful);
}
