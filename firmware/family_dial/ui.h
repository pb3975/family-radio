#pragma once
#include <stdint.h>

enum class Page { Player, Menu, Display, Settings, Clock, WifiConfirm, Wifi, WifiResult };
enum class ClockStyle { Quiet, Analog, Color };
inline ClockStyle clockStyle(uint8_t value) { return value < 3 ? (ClockStyle)value : ClockStyle::Color; }

enum class VisualMode { Artwork, Colorful };
enum class MenuAction { None, Songs, Artists, Playlists, Sources, Shuffle, WifiPrepare, WifiStart, WifiStop, WifiExit };

struct LocalUI {
  Page page = Page::Player;
  VisualMode mode = VisualMode::Artwork;
  ClockStyle clock = ClockStyle::Color;
  MenuAction pending = MenuAction::None;
  int selected = 0;
  bool spotify = false;
  bool open() const { return page != Page::Player; }
  void reset() { page = Page::Player; selected = 0; pending = MenuAction::None; }
  void menu() { page = Page::Menu; selected = 0; }
  int count() const { return page == Page::WifiResult ? 1 : page == Page::Clock ? 4 : page == Page::Settings ? 3 : page == Page::WifiConfirm ? 2 : page == Page::Wifi ? 5 : page == Page::Menu ? (spotify ? 8 : 7) : 3; }
  int firstVisible() const { return selected > 2 ? selected-2 : 0; }
  void turn(int steps) {
    selected += steps;
    if (selected < 0) selected = 0;
    if (selected >= count()) selected = count()-1;
  }
  void choose() {
    if (page == Page::Menu) {
      int sourceRow=spotify?3:2, shuffleRow=sourceRow+1;
      if(selected==0) pending=MenuAction::Songs;
      else if(selected==1) pending=MenuAction::Artists;
      else if(spotify && selected==2) pending=MenuAction::Playlists;
      else if(selected==sourceRow) pending=MenuAction::Sources;
      else if(selected==shuffleRow) pending=MenuAction::Shuffle;
      if(pending!=MenuAction::None) page=Page::Player;
      else page=selected==shuffleRow+1?Page::Display:selected==shuffleRow+2?Page::Settings:Page::Player;
    } else if (page == Page::Display) {
      if (selected < 2) { mode = selected == 0 ? VisualMode::Artwork : VisualMode::Colorful; page = Page::Player; }
      else page = Page::Menu;
    } else if (page == Page::Settings) {
      if(selected==0){page=Page::WifiConfirm;pending=MenuAction::WifiPrepare;}
      else page=selected==1?Page::Clock:Page::Menu;
    } else if(page==Page::Clock) {
      if(selected<3)clock=clockStyle(selected);
      page=Page::Settings;
    } else if(page==Page::WifiConfirm) {
      if(selected==1){page=Page::Wifi;pending=MenuAction::WifiStart;}
      else {page=Page::Settings;pending=MenuAction::WifiExit;}
    } else if(page==Page::WifiResult) {page=Page::Settings;pending=MenuAction::WifiExit;
    } else if(page==Page::Wifi) {
      if(selected<2){++selected;return;}
      if(selected==2){selected=3;return;}
      if(selected==3)pending=MenuAction::WifiStop;
      else{page=Page::Settings;pending=MenuAction::WifiExit;}
    }
    selected = 0;
  }
  const char* menuLabel(int item, bool shuffle) const {
    if(item==0)return "Songs";
    if(item==1)return "Artists";
    if(spotify && item==2)return "Playlists";
    int sourceRow=spotify?3:2;
    if(item==sourceRow)return "Sources";
    if(item==sourceRow+1)return shuffle?"Shuffle: on":"Shuffle: off";
    if(item==sourceRow+2)return spotify?"Local display":"Display";
    if(item==sourceRow+3)return "Settings";
    return "Back";
  }
  bool touch(int x, int y) {
    if (!open()) {
      if (x >= 88 && x <= 152 && y >= 220 && y < 240) { menu(); return true; }
      return false;
    }
    if (x < 38 || x > 202) return false;
    if(page==Page::WifiResult) {if(y<150||y>=178)return false;choose();return true;}
    if(page==Page::Wifi) {
      if(selected<3)return false;
      int row=y>=68&&y<96?3:y>=100&&y<128?4:-1;
      if(row<0)return false;
      selected=row;choose();return true;
    }
    if(page==Page::WifiConfirm) {
      int row=y>=126&&y<154?0:y>=158&&y<186?1:-1;
      if(row<0)return false;
      selected=row;choose();return true;
    }
    int row =
      (y >= 68 && y < 96 ? 0 : y >= 100 && y < 128 ? 1 : y >= 132 && y < 160 ? 2 : -1);
    if (row < 0) return false;
    selected = row + firstVisible(); choose(); return true;
  }
};

// Hold both ends, then restart. Unsigned time arithmetic also handles millis wrap.
inline int marqueeOffset(int width, int viewport, uint32_t age) {
  if (width <= viewport) return 0;
  uint32_t travel = uint32_t(width-viewport)*50; // 20 pixels per second.
  uint32_t phase = age % (travel+2800);
  if (phase <= 1400) return 0;
  if (phase >= 1400+travel) return width-viewport;
  return (phase-1400)/50;
}

struct VolumeIndicator {
  bool active = false;
  uint32_t changedAt = 0;
  void reset() { active = false; }
  void changed(uint32_t now) { active = true; changedAt = now; }
  int brightness(uint32_t now) {
    if (!active) return 0;
    uint32_t age = now-changedAt;
    if (age < 1800) return 255;
    if (age >= 2300) { active = false; return 0; }
    return ((2300-age)*15+499)/500*17;
  }
};
