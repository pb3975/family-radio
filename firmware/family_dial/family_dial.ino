#include <M5Dial.h>
#include <errno.h>
#include <Preferences.h>
#include <math.h>
#include "ui.h"
#include "wifi_ui.h"
#include "gestures.h"
#include "visuals.h"
#include "clock.h"

// B8 TSV protocol. USB only; board library owns internal pin assignments.
static char input[1153], session[17] = "", title[65] = "Local song";
static size_t used = 0;
static bool dropping = false, connected = false, everConnected = false;
static char cursorTitle[65] = "Local song", neighborTitles[4][65] = {};
static bool browsing = false, browsePending = false, shuffleEnabled = false;
static char browsePendingMode='B', browseMode='N', currentArtist[65]="Demo Artist", scopeLabel[65]="All songs";
static uint32_t browserToken=0;
static char source='L', catalogStatus[9]="READY";
static int browseCount=1;
static int currentIndex = 0, cursorIndex = 0, songCount = 1, elapsed = 0, duration = 0;
static FaceGesture face;
static bool menuTouchCaptured = false;
static uint32_t suppressTouchUntil = 0, suppressFaceUntil = 0, browsePendingAt = 0;
static bool helloSent = false;
static uint32_t lastState = 0, lastHello = 0, lastHeartbeat = 0, sequence = 0;
static long lastPosition = 0;
static long lastInputPosition = 0;
static int volume = 0, ceiling = 40;
static String playstate = "STOPPED", errorCode = "-", lastScreen;
static M5Canvas frame(&M5Dial.Display);
static bool frameReady = false;
static Artwork artwork;
static uint8_t levels[12] = {};
static LocalUI ui;
static WifiUI wifi;
static Preferences preferences;
static VolumeIndicator volumeIndicator;
static IdleClock idleClock;
static long localMinute = 0;

bool number(const char* s, long low, long high, long& value) {
  if (!s || !*s) return false;
  for (const char* p = s; *p; ++p) if (*p < '0' || *p > '9') return false;
  errno = 0;
  char* end;
  value = strtol(s, &end, 10);
  return !errno && !*end && value >= low && value <= high;
}

void receive(char* line) {
  if (!connected && !helloSent) return;
  if (!strncmp(line,"B8\tW\t",5)) {
    size_t packetBytes=strlen(line);
    struct Erase {char* p;size_t n;~Erase(){WifiUI::erase(p,n);}} secret{line,packetBytes};
    if(!connected || (ui.page!=Page::Wifi && ui.page!=Page::WifiConfirm && ui.page!=Page::WifiResult))return;
    char* f[12];char* part=line;
    for(int i=0;i<12;++i){f[i]=part;char* tab=strchr(part,'\t');if(i==11){if(tab)return;}else{if(!tab)return;*tab=0;part=tab+1;}}
    if(strcmp(f[2],session))return;
    if(wifi.receive(f,millis())) {
      if(ui.page==Page::WifiConfirm && strcmp(wifi.state,"IDLE")) {ui.page=Page::Wifi;ui.selected=0;}
      if(wifi.terminal()){ui.page=Page::WifiResult;ui.selected=0;}
      lastScreen="";
    }
    return;
  }
  if (!strncmp(line, "B8\tA\t", 5)) {
    if (!connected) return;
    char* fields[7]; char* part=line;
    for (int i=0;i<7;++i) {
      fields[i]=part; char* tab=strchr(part,'\t');
      if (i==6) { if(tab) return; }
      else { if(!tab) return; *tab=0; part=tab+1; }
    }
    long index,offset;
    if (number(fields[3],1,2147483647,index) && number(fields[5],0,Artwork::BYTES,offset))
    {
      bool identity=!strcmp(artwork.session,fields[2]) && artwork.index==index && !strcmp(artwork.key,fields[4]);
      bool valid=identity && strlen(fields[6])>0 && strlen(fields[6])<=1024 && strlen(fields[6])%2==0 && Artwork::hexString(fields[6],strlen(fields[6])) && offset+strlen(fields[6])/2<=Artwork::BYTES;
      if(valid) {
        artwork.chunk(fields[2],index,fields[4],offset,fields[6]);
        char ack[80];
        int length=artwork.ready ? snprintf(ack,sizeof(ack),"B8\tC\t%s\t%d\t%s\n",session,currentIndex,artwork.key) :
          snprintf(ack,sizeof(ack),"B8\tO\t%s\t%d\t%s\t%u\n",session,currentIndex,artwork.key,(unsigned)artwork.used);
        if(Serial && Serial.availableForWrite()>=length) Serial.write((uint8_t*)ack,length);
      }
    }
    return; // Artwork never refreshes the control heartbeat.
  }
  char* fields[29];
  char* cursor = line;
  for (int i = 0; i < 29; ++i) {
    fields[i] = cursor;
    char* tab = strchr(cursor, '\t');
    if (i == 28) { if (tab) return; }
    else { if (!tab) return; *tab = 0; cursor = tab + 1; }
  }
  if (strcmp(fields[0], "B8") || strcmp(fields[1], "S") || strlen(fields[2]) != 16) return;
  for (const char* p = fields[2]; *p; ++p) if (!isxdigit((unsigned char)*p)) return;
  String state = fields[3];
  if (state != "STOPPED" && state != "PAUSED" && state != "PLAYING" && state != "ERROR" && state != "BUFFERING") return;
  long v, cap;
  long minute;
  struct tm date;
  if (!number(fields[28], 0, 68374079, minute) || (minute && !clockDate(minute,date))) return;
  if (!number(fields[4], 0, 100, v) || !number(fields[5], 0, 100, cap) || v > cap) return;
  if (!*fields[6] || strlen(fields[6]) > 64 || strlen(fields[7]) > 16) return;
  for (const char* p = fields[6]; *p; ++p) if (*p < 32 || *p > 126) return;
  long cur, browse, count, pos, len, items, token, shuffle;
  if (strcmp(fields[8], "N") && strcmp(fields[8], "B") && strcmp(fields[8], "A") && strcmp(fields[8], "P") && strcmp(fields[8], "R")) return;
  if (!number(fields[21],0,20000,items) || !number(fields[20],1,2147483647,token) ||
      !number(fields[19],0,1,shuffle)) return;
  for(int i=17;i<=18;++i) {
    if(!*fields[i] || strlen(fields[i])>64) return;
    for(const char* p=fields[i];*p;++p) if(*p<32 || *p>126) return;
  }
  if (!number(fields[11], 0, 20000, count) || !number(fields[9], 0, 2147483647, cur) ||
      (count==0 ? cur!=0 : cur==0) || !number(fields[10], 0, items?items-1:0, browse) || !number(fields[12], 0, 999999, pos) ||
      !number(fields[13], 0, 999999, len) || !*fields[14] || strlen(fields[14]) > 64) return;
  for (const char* p = fields[14]; *p; ++p) if (*p < 32 || *p > 126) return;
  if (strcmp(fields[7], "-") && strcmp(fields[7], "SOURCE") && strcmp(fields[7], "STARTUP") &&
      strcmp(fields[7], "OUTPUT") && strcmp(fields[7], "PLAYER") && strcmp(fields[7], "ACCOUNT") &&
      strcmp(fields[7], "NETWORK") && strcmp(fields[7], "CATALOG") && strcmp(fields[7], "REVOKED") && strcmp(fields[7], "EXPIRED")) return;
  if ((strcmp(fields[15], "-") && !Artwork::hexString(fields[15],8)) || !Artwork::hexString(fields[16],24)) return;
  if((strcmp(fields[26],"L") && strcmp(fields[26],"S")) ||
      (strcmp(fields[27],"READY") && strcmp(fields[27],"LOADING") && strcmp(fields[27],"EMPTY") &&
       strcmp(fields[27],"OFFLINE") && strcmp(fields[27],"AUTH") && strcmp(fields[27],"ERROR"))) return;
  const int neighborDelta[4]={-2,-1,1,2};
  for(int i=0;i<4;++i) {
    const char* label=fields[22+i];
    bool expected=fields[8][0]!='N' && browse+neighborDelta[i]>=0 && browse+neighborDelta[i]<items;
    if(strlen(label)>64 || bool(*label)!=expected) return;
    for(const char* p=label;*p;++p) if(*p<32 || *p>126) return;
  }
  artwork.bind(fields[2], cur, fields[15]);
  for (int i=0;i<12;++i) levels[i] = state == "PLAYING" ? (Artwork::hex(fields[16][i*2])<<4)|Artwork::hex(fields[16][i*2+1]) : 0;
  bool sameSession=connected && !strcmp(session,fields[2]);
  if(sameSession && volume!=v) volumeIndicator.changed(millis());
  else if(!sameSession) volumeIndicator.reset();
  if (strcmp(session, fields[2])) {
    idleClock.reset(millis());
    strcpy(session, fields[2]);
    sequence = 0;
    wifi.reset();
    face.reset(); browsePending = false; ui.reset();
    lastPosition = M5Dial.Encoder.read();
    lastInputPosition = lastPosition;
  }
  strcpy(title, fields[6]);
  strcpy(cursorTitle, fields[14]);
  for(int i=0;i<4;++i) strcpy(neighborTitles[i],fields[22+i]);
  source=fields[26][0];strcpy(catalogStatus,fields[27]);ui.spotify=source=='S';
  browseMode=fields[8][0]; browsing=browseMode!='N';
  if(browsing) ui.reset();
  browserToken=token; browseCount=items; shuffleEnabled=shuffle;
  strcpy(currentArtist,fields[17]); strcpy(scopeLabel,fields[18]);
  if (browseMode==browsePendingMode) browsePending = false;
  currentIndex = cur; cursorIndex = browse; songCount = count;
  elapsed = pos; duration = len;
  playstate = state;
  errorCode = fields[7];
  volume = v; ceiling = cap;
  localMinute = minute;
  connected = everConnected = true;
  lastState = millis();
}

void event(char kind, int value) {
  if (!connected || sequence >= 2147483647) return;
  char line[96];
  int n = snprintf(line, sizeof(line), "B8\tE\t%s\t%lu\t%c\t%d\t%c\t%lu\n", session,
                   (unsigned long)++sequence, kind, value, browseMode, (unsigned long)browserToken);
  // No retry queue: old gestures must never replay after a reconnect.
  if (Serial && Serial.availableForWrite() >= n) Serial.write((uint8_t*)line, n);
}

uint16_t rainbowColor(int hue, int brightness) {
  hue %= 1536;
  int section=hue/256, t=hue%256;
  int r=0,g=0,b=0;
  switch(section) {
    case 0:r=255;g=t;break; case 1:r=255-t;g=255;break;
    case 2:g=255;b=t;break; case 3:g=255-t;b=255;break;
    case 4:r=t;b=255;break; default:r=255;b=255-t;break;
  }
  return ((r*brightness/255>>3)<<11)|((g*brightness/255>>2)<<5)|(b*brightness/255>>3);
}

uint16_t dimColor(uint16_t color, int brightness) {
  return (((((color>>11)&31)*brightness/255)&31)<<11)|
         (((((color>>5)&63)*brightness/255)&63)<<5)|((color&31)*brightness/255);
}

void stroke(lgfx::LGFXBase& d,int x,int y,int endX,int endY,uint16_t color,int width=2) {
  int dx=abs(endX-x), sx=x<endX?1:-1;
  int dy=-abs(endY-y), sy=y<endY?1:-1, error=dx+dy;
  while(true) {
    d.fillRect(x-width/2,y-width/2,width,width,color);
    if(x==endX && y==endY) break;
    int twice=2*error;
    if(twice>=dy) { error+=dy;x+=sx; }
    if(twice<=dx) { error+=dx;y+=sy; }
  }
}

void sideArc(lgfx::LGFXBase& d,bool left,int segments,uint16_t fill,int brightness=255) {
  for(int step=0;step<=160;++step) {
    float angle=(left?140+step*0.5f:-40+step*0.5f)*0.01745329252f;
    uint16_t color=dimColor(step<segments?fill:DARKGREY,brightness);
    for(int radius=98;radius<=101;++radius)
      d.drawPixel(120+lroundf(cosf(angle)*radius),108+lroundf(sinf(angle)*radius),color);
  }
}

void skipIcon(lgfx::LGFXBase& d,int center,bool next) {
  for(int col=0;col<=10;++col) {
    int x=next?center+5-col:center-5+col, half=col*7/10;
    d.fillRect(x,190-half,1,half*2+1,WHITE);
  }
  d.fillRect(next?center+9:center-11,183,3,15,WHITE);
}

void sourceIcon(lgfx::LGFXBase& d) {
  if(source=='S') {
    for(int row=0;row<3;++row) {
      stroke(d,58+row,208+row*4,66,206+row*4,WHITE);
      stroke(d,66,206+row*4,75-row,209+row*4,WHITE);
    }
    return;
  }
  d.drawRect(57,207,19,13,WHITE);
  d.fillRect(57,204,9,3,WHITE);
  d.fillRect(59,206,6,2,WHITE);
}

void shuffleIcon(lgfx::LGFXBase& d) {
  stroke(d,165,208,181,218,WHITE);
  stroke(d,165,218,181,208,WHITE);
  stroke(d,177,208,181,208,WHITE);stroke(d,181,208,181,212,WHITE);
  stroke(d,177,218,181,218,WHITE);stroke(d,181,214,181,218,WHITE);
}

void playbackIcon(lgfx::LGFXBase& d,const String& state) {
  if(state=="PLAYING") {
    for(int col=0;col<=9;++col) {int half=(9-col)*6/9;d.fillRect(91+col,212-half,1,half*2+1,WHITE);}
  } else if(state=="PAUSED") {
    d.fillRect(91,206,3,13,WHITE);d.fillRect(98,206,3,13,WHITE);
  } else d.fillRect(91,208,10,10,WHITE);
}

void colorfulVisualizer(lgfx::LGFXBase& d,bool playing) {
  // Layered source-RMS petals, inner spokes and bright tips. Silence has no invented motion.
  for(int bar=0;bar<72;++bar) {
    int index=bar/6, fraction=bar%6;
    int sample=playing?(levels[index]*(6-fraction)+levels[(index+1)%12]*fraction)/6:0;
    float angle=bar*6.28318530718f/72-1.57079632679f;
    float dx=cosf(angle),dy=sinf(angle);
    int inner=30-sample*28/255, outer=33+sample*31/255;
    int hue=bar*1536/72;
    for(int radius=inner;radius<=outer;++radius) {
      int brightness=sample?((radius==outer || radius==inner)?255:100+155*sample/255):55;
      uint16_t color=rainbowColor(hue,brightness);
      for(int side=-1;side<=1;++side)
        d.drawPixel(120+lroundf(dx*radius-dy*side),108+lroundf(dy*radius+dx*side),color);
    }
    if(sample) {
      // Opposing history samples create a second inner flower without a time-driven phase.
      int inward=levels[(bar*12/72+6)%12]*22/255;
      uint16_t color=rainbowColor(hue+256,90+sample*165/255);
      stroke(d,120+lroundf(dx*inward),108+lroundf(dy*inward),
             120+lroundf(dx*(inner+3)),108+lroundf(dy*(inner+3)),color,1);
    }
  }
}

void wifiLabel(lgfx::LGFXBase& d,const char* value,int y,int width=140) {
  char safe[65]={};size_t i=0;
  for(;value[i]&&i<64;++i)safe[i]=((unsigned char)value[i]>=32&&(unsigned char)value[i]<=126)?value[i]:'?';
  while(d.textWidth(safe)>width && i>3){safe[--i]=0;safe[i-1]='.';safe[i-2]='.';safe[i-3]='.';}
  d.drawString(safe,120,y);
}

void drawWifi(lgfx::LGFXBase& d) {
  d.setTextColor(CYAN,BLACK);
  if(ui.page==Page::WifiConfirm) {
    d.drawString("WI-FI SETUP",120,34);d.setTextColor(WHITE,BLACK);
    d.drawString("Temporarily",120,74);d.drawString("disconnect Wi-Fi?",120,96);
    const char* rows[]={"Back","Start setup"};
    for(int i=0;i<2;++i){if(ui.selected==i)d.drawRoundRect(38,126+i*32,164,28,6,CYAN);d.drawString(rows[i],120,140+i*32);}
    d.setTextColor(LIGHTGREY,BLACK);d.drawString("Saved networks stay",120,206);return;
  }
  if(ui.page==Page::WifiResult) {
    d.drawString(!strcmp(wifi.state,"CONNECTED")?"CONNECTED":"WI-FI SETUP",120,38);
    d.setTextColor(WHITE,BLACK);
    d.drawString(!strcmp(wifi.state,"CONNECTED")?"Wi-Fi saved":!strcmp(wifi.state,"EXPIRED")?"Setup timed out":!strcmp(wifi.state,"CANCELLED")?"Setup closed":"Setup unavailable",120,94);
    d.drawString(!strcmp(wifi.state,"CONNECTED")?"Old networks kept":"Try setup again",120,120);
    d.drawRoundRect(38,150,164,28,6,CYAN);d.drawString("Back",120,164);return;
  }
  if(ui.selected>=3) {
    d.drawString("WI-FI OPTIONS",120,38);d.setTextColor(WHITE,BLACK);
    const char* rows[]={"Cancel setup","Back"};
    for(int i=0;i<2;++i){if(ui.selected==i+3)d.drawRoundRect(38,68+i*32,164,28,6,CYAN);d.drawString(rows[i],120,82+i*32);}
    d.setTextColor(LIGHTGREY,BLACK);d.drawString("Back keeps setup on",120,164);return;
  }
  if(wifi.hasAccess() && ui.selected<2) {
    d.drawString(ui.selected==0?(!strcmp(wifi.state,"FAILED")?"TRY AGAIN":"SCAN TO JOIN"):"OPEN PAGE",120,24);
    d.qrcode(ui.selected==0?wifi.payload:"http://192.168.4.1/",38,38,164,4,true);
    d.setTextColor(WHITE,BLACK);wifiLabel(d,ui.selected==0?wifi.ssid:"192.168.4.1",213);
    d.setTextColor(LIGHTGREY,BLACK);d.drawString("Turn",120,230);return;
  }
  if(wifi.hasAccess() && ui.selected==2) {
    d.drawString("CONNECTION DETAILS",120,34);d.setTextColor(WHITE,BLACK);
    wifiLabel(d,wifi.ssid,64,172);
    d.setTextColor(LIGHTGREY,BLACK);d.drawString("Password",120,88);d.setTextColor(WHITE,BLACK);
    char line[22]={};size_t length=strlen(wifi.password);
    size_t rows=(length+20)/21,columns=(length+rows-1)/rows;
    for(size_t at=0;at<length;at+=columns){memset(line,0,sizeof(line));strncpy(line,wifi.password+at,columns);d.drawString(line,120,110+(at/columns)*22);}
    memset(line,0,sizeof(line));
    d.setTextColor(LIGHTGREY,BLACK);d.drawString("192.168.4.1",120,184);d.drawString("Turn for options",120,206);return;
  }
  d.drawString("WI-FI SETUP",120,38);d.setTextColor(WHITE,BLACK);
  d.drawString(!strcmp(wifi.state,"CONNECTING")?"Joining network":!strcmp(wifi.state,"STOPPING")?"Closing setup":"Starting setup",120,94);
  d.drawString("Please wait",120,120);d.setTextColor(LIGHTGREY,BLACK);
  d.drawString("Turn for options",120,182);
}

void drawClock(lgfx::LGFXBase& d) {
  struct tm date;
  if(!clockDate(localMinute,date))return;
  const uint16_t cream=0xef5b, amber=0xdd2c, muted=0x9cf2;
  const char* days[]={"SUNDAY","MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY"};
  const char* months[]={"JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"};
  char dateText[20], timeText[8];
  snprintf(dateText,sizeof(dateText),"%d %s",date.tm_mday,months[date.tm_mon]);
  snprintf(timeText,sizeof(timeText),"%02d:%02d",date.tm_hour,date.tm_min);
  if(ui.clock==ClockStyle::Analog) {
    for(int mark=0;mark<60;++mark) {
      if(mark%15==0)continue;
      float a=mark*0.104719755f-1.570796327f;
      int inner=mark%5?100:94;
      stroke(d,120+lroundf(cosf(a)*inner),120+lroundf(sinf(a)*inner),120+lroundf(cosf(a)*104),120+lroundf(sinf(a)*104),mark%5?0x52aa:cream,mark%5?1:3);
    }
    d.setTextColor(cream,BLACK);
    d.drawString("12",120,42);d.drawString("3",199,120);d.drawString("6",120,200);d.drawString("9",41,120);
    d.setTextColor(muted,BLACK);d.drawString(dateText,120,161);
    float hour=(date.tm_hour%12+date.tm_min/60.0f)*0.523598776f-1.570796327f;
    float minute=date.tm_min*0.104719755f-1.570796327f;
    stroke(d,120,120,120+lroundf(cosf(hour)*54),120+lroundf(sinf(hour)*54),cream,7);
    stroke(d,120,120,120+lroundf(cosf(minute)*77),120+lroundf(sinf(minute)*77),amber,4);
    d.fillRoundRect(114,114,12,12,6,amber);
    return;
  }
  if(ui.clock==ClockStyle::Color) {
    const uint16_t colors[]={0xf5cc,0xe3f4,0xa4dd,0x65db,0x7e76,0xd6cf};
    for(int step=0;step<360;++step) {
      if(step%60>50)continue;
      float angle=(step-90)*0.01745329252f;
      stroke(d,120+lroundf(cosf(angle)*100),120+lroundf(sinf(angle)*100),120+lroundf(cosf(angle)*106),120+lroundf(sinf(angle)*106),colors[step/60],2);
    }
    d.setTextColor(0xa4dd,BLACK);d.drawString(days[date.tm_wday],120,68);
    d.setTextSize(3);
    char digits[3];snprintf(digits,sizeof(digits),"%02d",date.tm_hour);
    d.setTextColor(0xf5cc,BLACK);d.drawString(digits,80,119);
    d.setTextColor(cream,BLACK);d.drawString(":",120,119);
    snprintf(digits,sizeof(digits),"%02d",date.tm_min);
    d.setTextColor(0x8eb8,BLACK);d.drawString(digits,160,119);d.setTextSize(1);
    d.setTextColor(muted,BLACK);d.drawString(dateText,120,165);
    stroke(d,110,186,120,191,0xe3f4,2);stroke(d,120,191,130,186,0xe3f4,2);
  } else {
    d.setTextColor(amber,BLACK);d.drawString(days[date.tm_wday],120,72);
    d.setTextSize(3);d.setTextColor(cream,BLACK);d.drawString(timeText,120,120);d.setTextSize(1);
    d.setTextColor(muted,BLACK);d.drawString(dateText,120,173);
    stroke(d,106,202,134,202,0x6287,2);
  }
}

void draw() {
  uint32_t now=millis();
  static uint32_t drawnState=UINT32_MAX, drawnTick=UINT32_MAX, titleSince=0;
  static String previousTitle;
  static int previousIndex=-1, previousPage=-1;
  static bool drawnCover=false, drawnConnected=false, drawnBrowsing=false;
  static int drawnPage=-1, drawnMode=-1, drawnSelection=-1;
  static int drawnClock=-1;
  static long drawnMinute=-1;
  static bool drawnIdle=false;
  int page=(int)ui.page, mode=(int)ui.mode;
  uint32_t tick=now/50;
  if(drawnState==lastState && drawnTick==tick && drawnCover==artwork.ready && drawnConnected==connected && drawnBrowsing==browsing && drawnPage==page && drawnMode==mode && drawnSelection==ui.selected && drawnClock==(int)ui.clock && drawnMinute==localMinute && drawnIdle==idleClock.visible) return;
  drawnState=lastState; drawnTick=tick; drawnCover=artwork.ready; drawnConnected=connected; drawnBrowsing=browsing;
  drawnPage=page; drawnMode=mode; drawnSelection=ui.selected;
  drawnClock=(int)ui.clock;drawnMinute=localMinute;drawnIdle=idleClock.visible;
  String status = connected ? (browsing ? (browseMode=='A'?"CHOOSE ARTIST":browseMode=='P'?"CHOOSE PLAYLIST":browseMode=='R'?"CHOOSE SOURCE":"CHOOSE SONG") : playstate) : (everConnected ? "DISCONNECTED" : "CONNECTING");
  String label = browsing ? cursorTitle : title;
  int index=browsing ? cursorIndex : currentIndex;
  int titlePage=page*2+(browsing?1:0);
  if(label!=previousTitle || index!=previousIndex || titlePage!=previousPage) {
    previousTitle=label; previousIndex=index; previousPage=titlePage; titleSince=now;
  }
  lgfx::LGFXBase* target = frameReady ? (lgfx::LGFXBase*)&frame : &M5Dial.Display;
  auto& d = *target;
  d.setFont(&fonts::Font2);
  int viewport=browsing?172:112;
  int textWidth=d.textWidth(label);
  int offset=ui.open()?0:marqueeOffset(textWidth,viewport,uint32_t(now-titleSince));
  int volumeBrightness=connected && !browsing && !ui.open()?volumeIndicator.brightness(now):0;
  String screen = status + "|" + label + "|" + String(volume) + "|" + errorCode + "|" + String(cursorIndex) + "|" + String(currentIndex) + "|" + String(songCount) + "|" + String(elapsed) + "|" + String(duration) + "|" + String(artwork.ready)+"|"+String(shuffleEnabled)+"|"+String(browserToken)+"|"+String(scopeLabel)+"|"+String(page)+"|"+String(mode)+"|"+String(ui.selected)+"|"+String(offset)+"|"+String(volumeBrightness)+"|"+String(source=='S'?"S":"L")+"|"+String(catalogStatus);
  screen += "|";screen += String(wifi.revision);screen += "|";screen += String(wifi.remaining);
  screen += "|";screen += String(wifi.hasAccess());screen += "|";screen += wifi.state;
  screen += "|";screen += String(idleClock.visible);screen += "|";screen += String((int)ui.clock);
  screen += "|";screen += String((int)localMinute);
  for (int i=0;i<12;++i) { screen += ":"; screen += String(levels[i]); }
  if(browsing) for(int i=0;i<4;++i) {screen+="|";screen+=neighborTitles[i];}
  if(idleClock.visible) screen=String("CLOCK|")+String((int)localMinute)+"|"+String((int)ui.clock);
  if (screen == lastScreen) return;
  lastScreen = screen;
  uint16_t color = !connected ? ORANGE : (playstate == "ERROR" ? RED : CYAN);
  d.fillScreen(BLACK);
  d.setTextDatum(middle_center);
  d.setTextColor(WHITE, BLACK);
  if(idleClock.visible) drawClock(d);
  else if(ui.open()) {
    if(ui.page==Page::Wifi || ui.page==Page::WifiConfirm || ui.page==Page::WifiResult) drawWifi(d);
    else {
      d.setTextColor(CYAN,BLACK);
      d.drawString(ui.page==Page::Menu?"MENU":ui.page==Page::Clock?"IDLE CLOCK":ui.page==Page::Display?(source=='S'?"LOCAL DISPLAY":"DISPLAY"):"SETTINGS",120,38);
      if(ui.page==Page::Settings) {
        const char* rows[]={"Wi-Fi setup","Idle clock","Back"};
        for(int i=0;i<3;++i){if(ui.selected==i)d.drawRoundRect(38,68+i*32,164,28,6,CYAN);d.setTextColor(WHITE,BLACK);d.drawString(rows[i],120,82+i*32);}
        d.setTextColor(LIGHTGREY,BLACK);
        d.drawString(source=='S'?"Source: Spotify":"Source: local",120,174);
      } else {
        const char* displayRows[]={"Art + history","Colorful","Back"};
        const char* clockRows[]={"Quiet digital","Station clock","Color break","Back"};
        int first=ui.firstVisible();
        for(int row=0;row<3;++row) {
          int item=first+row;
          if(item>=ui.count())break;
          int y=82+row*32;
          if(ui.selected==item) d.drawRoundRect(38,y-14,164,28,6,CYAN);
          bool active=(ui.page==Page::Display && item<2 && (int)ui.mode==item) || (ui.page==Page::Clock && item<3 && (int)ui.clock==item);
          d.setTextColor(active?CYAN:WHITE,BLACK);
          d.drawString(ui.page==Page::Menu?ui.menuLabel(item,shuffleEnabled):ui.page==Page::Clock?clockRows[item]:displayRows[item],120,y);
        }
      }
      d.setTextColor(LIGHTGREY,BLACK);
      if(ui.page==Page::Menu) d.drawString(String(ui.selected+1)+" / "+ui.count(),120,174);
      d.drawString("Turn + press, or tap",120,196);
    }
  } else {
    if (browsing) {
      d.setTextColor(color,BLACK);d.drawString(browseMode=='A'?"ARTISTS":browseMode=='P'?"PLAYLISTS":browseMode=='R'?"SOURCES":"SONGS",120,24);
      const int widths[5]={108,152,188,152,108}, heights[5]={20,22,28,22,20};
      const char* labels[5]={neighborTitles[0],neighborTitles[1],cursorTitle,neighborTitles[2],neighborTitles[3]};
      for(int row=0;row<5;++row) {
        if(!*labels[row]) continue;
        int y=58+row*28, width=widths[row], height=heights[row], left=120-width/2;
        bool selected=row==2;
        uint16_t background=selected?dimColor(CYAN,55):dimColor(WHITE,row==1||row==3?24:12);
        d.fillRoundRect(left,y-height/2,width,height,height/2,background);
        if(selected) d.drawRoundRect(left,y-height/2,width,height,height/2,CYAN);
        d.setTextColor(selected?WHITE:row==1||row==3?LIGHTGREY:DARKGREY,background);
        d.setClipRect(left+8,y-8,width-16,16);
        if(selected) d.drawString(label,textWidth<=172?120:left+8+textWidth/2-offset,y);
        else {
          String neighbor=labels[row];
          while(d.textWidth(neighbor)>width-16 && neighbor.length()>6) {
            int mid=neighbor.length()/2;neighbor=neighbor.substring(0,mid-1)+"..."+neighbor.substring(mid+3);
          }
          d.drawString(neighbor,120,y);
        }
        d.clearClipRect();
      }
      d.setTextColor(playstate=="ERROR"?color:LIGHTGREY,BLACK);
      d.drawString(playstate=="ERROR"?errorCode:browseCount?String(cursorIndex+1)+" / "+browseCount:String(catalogStatus),120,202);
      d.setTextColor(LIGHTGREY,BLACK);
      d.drawString("Hold: back",120,222);
    } else {
      d.setClipRect(64,16,112,16);
      d.drawString(label,textWidth<=112?120:64+textWidth/2-offset,24);
      d.clearClipRect();
      if(ui.mode==VisualMode::Colorful && source!='S') {
        colorfulVisualizer(d,connected && playstate=="PLAYING");
      } else {
        if(connected && artwork.ready) {
          d.setSwapBytes(false);
          d.pushImage(64,40,112,112,artwork.pixels);
        } else {
          d.fillRect(64,40,112,112,DARKGREY);
          d.fillRect(119,65,4,47,color); d.fillRect(119,65,25,4,color);
          d.fillRect(105,105,18,12,color);
        }
        if(source=='S') {
          d.setTextColor(LIGHTGREY,BLACK);d.setClipRect(54,154,132,18);
          String artist=currentArtist;
          while(d.textWidth(artist)>130 && artist.length()>8) artist=artist.substring(0,artist.length()-4)+"...";
          d.drawString(artist,120,162);d.clearClipRect();
          d.setTextColor(LIGHTGREY,BLACK);d.drawString("Spotify",120,178);
        }
        for(int i=0;source!='S' && i<12;++i) {
          int height=connected && playstate=="PLAYING" ? levels[i]*14/255 : 0;
          d.fillRect(61+i*10,170-height,7,height?height:1,height?color:DARKGREY);
        }
      }
      sideArc(d,false,duration>0?constrain((int)(161LL*elapsed/duration),0,161):0,CYAN);
      if(volumeBrightness) sideArc(d,true,volume*161/100,CYAN,volumeBrightness);
    }
    if(!browsing) {
      skipIcon(d,73,false);skipIcon(d,167,true);
      if(!connected || playstate=="ERROR" || errorCode!="-" || (source=='S' && songCount==0)) {
        d.setTextColor(color,BLACK);d.drawString(!connected?status:errorCode!="-"?errorCode:!strcmp(catalogStatus,"AUTH")?"Sign in":!strcmp(catalogStatus,"LOADING")?"Loading":!strcmp(catalogStatus,"OFFLINE")?"Offline":"No songs",120,212);
      } else {
        sourceIcon(d);playbackIcon(d,playstate);
        char clock[20];
        if(elapsed>=3600) snprintf(clock,sizeof(clock),"%dh%02d",elapsed/3600,elapsed%3600/60);
        else snprintf(clock,sizeof(clock),"%d:%02d",elapsed/60,elapsed%60);
        d.setTextColor(LIGHTGREY,BLACK);
        d.setClipRect(107,204,50,16);d.drawString(clock,132,212);d.clearClipRect();
        if(shuffleEnabled) shuffleIcon(d);
      }
      if(connected) for(int y=225;y<=233;y+=4) d.fillRect(112,y,17,2,WHITE);
    }
  }
  if(frameReady) frame.pushSprite(0,0);
}

void setup() {
  auto cfg = M5.config();
  M5Dial.begin(cfg, true, false); // Encoder enabled, RFID disabled.
  M5Dial.Touch.setHoldThresh(300); // wasClicked rejects holds and library-detected flicks.
  frame.setColorDepth(16);
  frameReady = frame.createSprite(240, 240) != nullptr;
  preferences.begin("family-ui",false);
  ui.mode=preferences.getUChar("visual",0)==1?VisualMode::Colorful:VisualMode::Artwork;
  ui.clock=clockStyle(preferences.getUChar("clock",2));
  Serial.setRxBufferSize(4096); // HWCDC default 256 bytes cannot absorb a display redraw.
  Serial.begin(115200); // FQBN enables USB CDC; no wait for a host.
  Serial.setTxTimeoutMs(0);
  lastPosition = M5Dial.Encoder.read();
  lastInputPosition = lastPosition;
  draw();
}

void loop() {
  M5Dial.update();
  uint32_t now = millis();
  if (!M5Dial.Touch.getCount()) menuTouchCaptured = false;
  if (browsePending && uint32_t(now-browsePendingAt) >= 1000) browsePending = false;
  if (connected && uint32_t(now - lastState) >= 3000) {
    connected = false;
    idleClock.reset(now);localMinute=0;
    volumeIndicator.reset();
    artwork.clear(); memset(levels,0,sizeof(levels));
    helloSent = false;
    wifi.reset();
    face.reset(); browsePending = false; ui.reset();
    session[0] = 0; browsing=false; browseMode='N'; browserToken=0;
    dropping = used || dropping;WifiUI::erase(input,sizeof(input));used = 0;
  }
  // Limit work each loop even if the peer continuously sends garbage.
  for (int budget = 0; budget < 2048 && Serial.available(); ++budget) {
    char c = Serial.read();
    if (c == '\n') {
      if (!dropping) { input[used] = 0; receive(input); }
      WifiUI::erase(input,sizeof(input));used = 0; dropping = false;
    } else if (!dropping) {
      if (used == 1152 || (c < 32 && c != '\t') || c > 126) { dropping = true; used = 0; }
      else input[used++] = c;
    }
  }
  if (!connected && uint32_t(now - lastHello) >= 1000) {
    const char* ready = "B8\tREADY\n";
    // Zero-timeout HWCDC flush drops stale bytes and re-arms transmission.
    if (Serial.availableForWrite() < 9) Serial.flush();
    else if (Serial && Serial.write((const uint8_t*)ready,9)==9) helloSent = true;
    lastHello = now;
  }
  if (connected && uint32_t(now - lastHeartbeat) >= 1000) {
    event('H', 0); lastHeartbeat = now;
  }
  long position = M5Dial.Encoder.read();
  bool buttonContact=M5Dial.BtnA.isPressed();
  bool touchContact=M5Dial.Touch.getCount() && !M5Dial.Touch.getDetail().wasReleased();
  bool gesture=position!=lastInputPosition || M5Dial.BtnA.wasPressed() || M5Dial.BtnA.wasReleased() || M5Dial.Touch.getCount();
  lastInputPosition=position;
  bool clockAllowed=connected && localMinute && !browsing && !browsePending && !ui.open() &&
    (playstate=="STOPPED" || playstate=="PAUSED") && errorCode=="-" &&
    !strcmp(catalogStatus,"READY") && !(source=='S' && songCount==0) && !wifi.pending;
  if(!idleClock.visible && (buttonContact || touchContact || menuTouchCaptured))clockAllowed=false;
  idleClock.tick(now,clockAllowed);
  bool wakeCaptured=idleClock.wake(now,gesture,buttonContact || touchContact);
  if(wakeCaptured) {
    lastPosition=position;face.reset();
    suppressTouchUntil=idleClock.lastGesture+350;suppressFaceUntil=idleClock.lastGesture+350;
  }
  // Library reports 4 counts/detent on original Dial. Preserve fractional motion.
  long steps = (position - lastPosition) / 4;
  if (steps && !wakeCaptured) {
    lastPosition += steps * 4;
    if(ui.open()) ui.turn(constrain(steps,-20L,20L));
    else if (!browsePending) event('K', constrain(steps, -20L, 20L));
  }
  if (!connected) lastPosition = position;
  if (!wakeCaptured && M5Dial.BtnA.wasPressed()) {
    face.press(now, connected && !browsePending && !menuTouchCaptured && (int32_t)(now-suppressFaceUntil) >= 0);
    suppressTouchUntil = now + 350;
  }
  if (!wakeCaptured && M5Dial.BtnA.isPressed()) {
    if (face.hold(now)) {
      if(ui.open()) {
        if(ui.page==Page::Wifi || ui.page==Page::WifiConfirm || ui.page==Page::WifiResult)ui.pending=MenuAction::WifiExit;
        ui.page=Page::Player;ui.selected=0;
      }
      else { event('L', browsing?-1:0); browsePendingMode=browsing?'N':'B'; browsePending = true; browsePendingAt = now; }
    }
    suppressTouchUntil = now + 350;
  }
  if (!wakeCaptured && M5Dial.BtnA.wasReleased()) {
    if (face.release() && !browsePending) {
      if(ui.open()) ui.choose();
      else if(!browsing || browseCount) event('P', browsing ? cursorIndex : (playstate=="PLAYING" || playstate=="BUFFERING"?0:1));
    }
    suppressTouchUntil = now + 350;
  }
  if (!wakeCaptured && M5Dial.Touch.getCount()) {
    auto touch = M5Dial.Touch.getDetail();
    if (menuTouchCaptured) {
      // The contact that opened the menu must not select a row on release.
      if(touch.wasReleased()) menuTouchCaptured=false;
      face.reset(); suppressFaceUntil=now+350;
    } else if (connected && !browsePending && !M5Dial.BtnA.isPressed() &&
        (int32_t)(now - suppressTouchUntil) >= 0) {
      // Opening local navigation does not need M5's release-time hold/flick test.
      if (!ui.open() && !browsing && touch.wasPressed() && ui.touch(touch.x,touch.y)) {
        menuTouchCaptured=true;
        face.reset(); suppressFaceUntil=now+350;
      } else if (touch.wasClicked()) {
        if(ui.open()) { ui.touch(touch.x,touch.y); face.reset(); suppressFaceUntil=now+350; }
        else {
          int skip = browsing?0:skipTarget(touch.x, touch.y);
          if (skip) { event('S', skip); face.reset(); suppressFaceUntil = now + 350; }
        }
      }
    }
  }
  if(ui.pending!=MenuAction::None) {
    MenuAction action=ui.pending;ui.pending=MenuAction::None;
    if(action==MenuAction::WifiPrepare)wifi.request('P');
    else if(action==MenuAction::WifiStart)wifi.request('S');
    else if(action==MenuAction::WifiStop)wifi.request('T');
    else if(action==MenuAction::WifiExit){
      if(wifi.pending && wifi.command=='T'){ui.page=Page::Wifi;ui.selected=0;}
      else wifi.request('X');
      WifiUI::erase(input,sizeof(input));if(used||dropping){used=0;dropping=true;}
    }
    else if(action==MenuAction::Shuffle) event('Q',shuffleEnabled?0:1);
    else { int kind=action==MenuAction::Artists?1:action==MenuAction::Playlists?2:action==MenuAction::Sources?3:0; event('L',kind);browsePendingMode=kind==1?'A':kind==2?'P':kind==3?'R':'B';browsePending=true;browsePendingAt=now; }
  }
  wifi.tick(now);
  bool wifiOpen=ui.page==Page::Wifi || ui.page==Page::WifiConfirm;
  if(wifiOpen && !wifi.pending && wifi.command && uint32_t(now-wifi.receivedAt)>=1000)wifi.request('Q');
  if(connected && wifi.due(now)) {
    char line[112];
    int n=snprintf(line,sizeof(line),"B8\tW\t%s\t%lu\t%lu\t%c\t%s\n",session,
      (unsigned long)++sequence,(unsigned long)wifi.requestId,wifi.command,*wifi.setupId?wifi.setupId:"-");
    if(Serial && Serial.availableForWrite()>=n && Serial.write((const uint8_t*)line,n)==(size_t)n)wifi.sentAt=now;
    if(!wifiOpen && wifi.command=='X' && wifi.sentAt){wifi.pending=false;wifi.clearAccess();}
  }
  static VisualMode savedMode=ui.mode;
  if(savedMode!=ui.mode) { preferences.putUChar("visual",(int)ui.mode); savedMode=ui.mode; }
  static ClockStyle savedClock=ui.clock;
  if(savedClock!=ui.clock) { preferences.putUChar("clock",(int)ui.clock); savedClock=ui.clock; }
  draw();
  delay(5);
}
