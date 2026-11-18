#pragma once
#include <stdint.h>
struct FaceGesture {
  bool armed = false, held = false;
  uint32_t pressed = 0;
  void reset() { armed = held = false; }
  void press(uint32_t now, bool connected) { armed = connected; held = false; pressed = now; }
  bool hold(uint32_t now) {
    if (!armed || held || uint32_t(now-pressed) < 600) return false;
    held = true; return true;
  }
  bool release() { bool tap = armed && !held; reset(); return tap; }
};
inline int skipTarget(int x, int y) {
  return y >= 176 && y <= 204 ? (x >= 38 && x <= 108 ? -1 : (x >= 132 && x <= 202 ? 1 : 0)) : 0;
}
