#pragma once
#include <stdint.h>
#include <time.h>

// Eligibility starts a fresh interval. Host snapshots do not count as activity.
struct IdleClock {
  bool eligible = false, visible = false, capturing = false;
  uint32_t since = 0, lastGesture = 0;
  void reset(uint32_t now) { eligible = visible = capturing = false; since = now; }
  void activity(uint32_t now) { since = now; visible = false; }
  void tick(uint32_t now, bool allowed) {
    if (!allowed || !eligible) activity(now);
    eligible = allowed;
    if (allowed && !capturing && uint32_t(now-since) >= 120000) visible = true;
  }
  bool wake(uint32_t now, bool gesture, bool contact) {
    if (visible && gesture) { activity(now); capturing = true; }
    if (!capturing) { if (gesture) activity(now); return false; }
    activity(now);
    if (gesture || contact) lastGesture = now;
    // Consume the release iteration too, including a release-time touch click.
    if (!contact && uint32_t(now-lastGesture) >= 350) capturing = false;
    return true;
  }
};

inline bool clockDate(long minute, struct tm& date) {
  if (minute < 28401120 || minute >= 68374080) return false; // 2024 through 2099.
  time_t seconds = (int64_t)minute * 60;
  return gmtime_r(&seconds, &date) != nullptr;
}
