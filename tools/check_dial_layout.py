#!/usr/bin/env python3
"""Run the actual firmware draw() with installed Font2 glyphs on a native canvas.
This checks synthetic layouts, not Dial rendering speed, touch or physical legibility.
"""
import argparse
from pathlib import Path
import subprocess
import tempfile
import struct
import zlib
import json
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--font', type=Path, default=Path.home()/'Arduino/libraries/M5GFX/src/lgfx/Fonts/Font16.h')
parser.add_argument('--qr-source', type=Path, help='Pinned M5GFX lgfx/utility directory')
parser.add_argument('--output', type=Path, default=Path('/tmp/family-radio-ui-curved'))
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
qr_source = args.qr_source or args.font.parent.parent/'utility'
ino = Path('firmware/family_dial/family_dial.ino').read_text()
firmware = '\n'.join(line for line in ino.splitlines() if not line.startswith('#include'))
shim = r'''
#include <string>
#include <map>
#include <vector>
#include <algorithm>
#include <cstdio>
#include <cassert>
#include <cstring>
#include <cstdint>
#include <cmath>
#include <cerrno>
#include <cstdlib>
#include "ui.h"
#include "wifi_ui.h"
#include "QR_HEADER"
#include "gestures.h"
#include "visuals.h"
#include "clock.h"
#define PROGMEM
#include "FONT_HEADER"
using std::min;
uint32_t fakeNow=0;
uint32_t millis(){return fakeNow;}
void delay(int){}
template<class T>T constrain(T n,T lo,T hi){return n<lo?lo:n>hi?hi:n;}
struct String: std::string {
 using std::string::string;
 String(std::string s):std::string(s){}
 String(int n):std::string(std::to_string(n)){}
 String substring(int start, int end=99999) const {return substr(start,std::min(end,(int)size())-start);}
 int length() const {return size();}
};
String operator+(String a, const String& b){a.append(b);return a;}
String operator+(String a, const char* b){a.append(b);return a;}
String operator+(String a, int b){a.append(std::to_string(b));return a;}
namespace fonts {int Font2;}
int middle_center=0;
// Pinned M5GFX v0.2.29 enum.hpp RGB765 palette.
enum {BLACK=0x0000,WHITE=0xffff,CYAN=0x07ff,RED=0xf800,ORANGE=0xfda0,LIGHTGREY=0xd69a,DARKGREY=0x7bef};
int encodeColor(uint16_t c){if(!c)return 0;int r=((c>>11)&31)*255/31,g=((c>>5)&63)*255/63,b=(c&31)*255/31;return 7+(r<<16)+(g<<8)+b;}
namespace lgfx {
struct LGFXBase {
 std::vector<int> pixels=std::vector<int>(240*240);int fg=7+0xffffff,bg=0,size=1;
 std::vector<std::string> labels;std::vector<int> labelY; bool swap=false;
 int clipX=0,clipY=0,clipW=240,clipH=240,drawCount=0;
 void setClipRect(int x,int y,int w,int h){clipX=x;clipY=y;clipW=w;clipH=h;}
 void clearClipRect(){setClipRect(0,0,240,240);}
 void drawPixel(int x,int y,uint16_t c){pixel(x,y,encodeColor(c));}
 void setColorDepth(int){}
 void* createSprite(int,int){return nullptr;}
 void setSwapBytes(bool v){swap=v;}
 // Match pinned M5GFX create_pc(): swap=false means big-endian source bytes.
 void pushImage(int x,int y,int w,int h,const uint16_t* data){for(int j=0;j<h;++j)for(int i=0;i<w;++i){uint16_t c=data[j*w+i];if(!swap)c=(c>>8)|(c<<8);int r=((c>>11)&31)*255/31,g=((c>>5)&63)*255/63,b=(c&31)*255/31;pixel(x+i,y+j,7+(r<<16)+(g<<8)+b);}}
 void pixel(int x,int y,int c){if(x>=0&&x<240&&y>=0&&y<240&&x>=clipX&&x<clipX+clipW&&y>=clipY&&y<clipY+clipH)pixels[y*240+x]=c;}
 void fillScreen(int c){++drawCount;std::fill(pixels.begin(),pixels.end(),encodeColor(c));labels.clear();labelY.clear();}
 void setTextSize(int n){size=n;}
 void setTextDatum(int){} void setFont(int*){} void setTextColor(int c,int background){fg=encodeColor(c);bg=encodeColor(background);}
 int textWidth(String s){int w=0;for(unsigned char c:s)w+=size*widtbl_f16[c-32];return w;}
 void drawString(String s,int cx,int cy){
   labels.push_back(s);labelY.push_back(cy);int x=cx-textWidth(s)/2, y=cy-8*size;
   for(int j=y;j<y+16*size;++j)for(int i=x;i<x+textWidth(s);++i)pixel(i,j,bg);
   for(unsigned char c:s){int w=widtbl_f16[c-32], stride=(w+6)/8;
    auto glyph=chrtbl_f16[c-32];
    for(int row=0;row<16;++row)for(int col=0;col<w;++col)
      if(glyph[row*stride+col/8]&(0x80>>(col%8)))for(int sy=0;sy<size;++sy)for(int sx=0;sx<size;++sx)pixel(x+col*size+sx,y+row*size+sy,fg);
    x+=w*size;
   }
 }
 void fillRect(int x,int y,int w,int h,int c){for(int i=x;i<x+w;++i)for(int j=y;j<y+h;++j)pixel(i,j,encodeColor(c));}
 void drawRect(int x,int y,int w,int h,int c){fillRect(x,y,w,1,c);fillRect(x,y+h-1,w,1,c);fillRect(x,y,1,h,c);fillRect(x+w-1,y,1,h,c);}
 bool inRounded(int x,int y,int w,int h,int r){
   if(x<0||y<0||x>=w||y>=h)return false;
   int cx=std::max(r,std::min(x,w-r-1)),cy=std::max(r,std::min(y,h-r-1));
   return (x-cx)*(x-cx)+(y-cy)*(y-cy)<=r*r;
 }
 void fillRoundRect(int x,int y,int w,int h,int r,int c){for(int j=0;j<h;++j)for(int i=0;i<w;++i)if(inRounded(i,j,w,h,r))pixel(x+i,y+j,encodeColor(c));}
 void drawRoundRect(int x,int y,int w,int h,int r,int c){for(int j=0;j<h;++j)for(int i=0;i<w;++i)if(inRounded(i,j,w,h,r)&&!inRounded(i-1,j-1,w-2,h-2,r-1))pixel(x+i,y+j,encodeColor(c));}
 void qrcode(const char* text,int x,int y,int w,uint8_t version,bool margin){
  bool rendered=false;
  for(;version<=40;++version){
   QRCode code;std::vector<uint8_t> data(lgfx_qrcode_getBufferSize(version));
   if(lgfx_qrcode_initText(&code,data.data(),version,0,text)!=0)continue;
   int thickness=w/code.size,line=code.size*thickness,offset=(w-line)/2;
   if(margin){int quiet=thickness*4;if(offset<quiet){thickness=(w-quiet*2)/code.size;line=code.size*thickness;offset=(w-line)/2;}}
   assert(thickness>0);fillRect(x,y,w,w,WHITE);
   for(int row=0;row<code.size;++row)for(int col=0;col<code.size;++col)
    fillRect(x+offset+col*thickness,y+offset+row*thickness,thickness,thickness,lgfx_qrcode_getModule(&code,col,row)?BLACK:WHITE);
   rendered=true;break;
  }assert(rendered);
 }
 void pushSprite(int,int){}
};}
struct EncoderMock {long position=0;long read(){return position;}};
struct ButtonMock {
 bool down=false,pressed=false,released=false;
 bool wasPressed(){return pressed;}bool wasReleased(){return released;}bool isPressed(){return down;}
};
struct TouchDetail {int x=0,y=0;bool clicked=false,pressed=false,released=false,down=false;bool wasClicked(){return clicked;}bool wasPressed(){return pressed;}bool wasReleased(){return released;}};
struct TouchMock {TouchDetail detail;void setHoldThresh(int){}int getCount(){return detail.down||detail.released?1:0;}TouchDetail getDetail(){return detail;}};
struct DialMock {
 lgfx::LGFXBase Display;EncoderMock Encoder;ButtonMock BtnA;TouchMock Touch;
 void begin(int,bool,bool){} void update(){}
} M5Dial;
struct {int config(){return 0;}} M5;
struct M5Canvas:lgfx::LGFXBase {M5Canvas(lgfx::LGFXBase*){}};
struct Preferences {
 std::map<std::string,uint8_t> values;int writes=0;bool begin(const char*,bool){return true;}
 uint8_t getUChar(const char* key,int fallback){return values.count(key)?values[key]:fallback;}
 void putUChar(const char* key,int v){values[key]=v;++writes;}
};
struct SerialMock {
 std::string output,input,pending;
 bool plugged=true,usbConnected=true,txArmed=false;int freeBytes=256,nextWriteLimit=-1,writeCalls=0,timeoutMs=100;
 void setRxBufferSize(int){}void setTxTimeoutMs(int value){timeoutMs=value;}void begin(int){}
 int available(){return input.size();}int availableForWrite(){return freeBytes;}
 // Arduino-ESP32 3.3.11 HWCDC: the readiness probe re-arms TX when the
 // host has returned, while availableForWrite() only reports ring space.
 operator bool(){if(!plugged){usbConnected=false;return false;}if(!usbConnected)txArmed=true;return usbConnected;}
 void hostPickup(){if(plugged&&txArmed){usbConnected=true;freeBytes=256;txArmed=false;output+=pending;pending.clear();}}
 void flush(){assert(timeoutMs==0);bool ready=*this;if(ready){txArmed=true;usbConnected=false;}pending.clear();freeBytes=256;}
 char read(){char c=input[0];input.erase(0,1);return c;}
 size_t print(const char* s){return write((const uint8_t*)s,strlen(s));}
 size_t write(const uint8_t* s,int n){++writeCalls;if(!*this){pending.append((const char*)s,n);if(pending.size()>256)pending.erase(0,pending.size()-256);freeBytes=256-pending.size();txArmed=true;return n;}int sent=nextWriteLimit<0||nextWriteLimit>=n?n:0;nextWriteLimit=-1;output.append((const char*)s,sent);return sent;}
} Serial;
'''.replace('FONT_HEADER', str(args.font.resolve())).replace('QR_HEADER', str((qr_source/'lgfx_qrcode.h').resolve()))
main = r'''
void saveFrame(const char* root,const std::string& state) {
 auto& d=M5Dial.Display;
 for(int y=0;y<240;++y)for(int x=0;x<240;++x)
   if(d.pixels[y*240+x]) assert((x-120)*(x-120)+(y-120)*(y-120)<=120*120);
 unsigned char colors[7][3]={{0,0,0},{255,255,255},{0,220,255},{255,50,50},{255,160,30},{190,190,190},{65,65,65}};
 std::string name=std::string(root)+"/"+state+".ppm";
 FILE* f=fopen(name.c_str(),"wb");assert(f);fprintf(f,"P6\n240 240\n255\n");
 for(int c:d.pixels){if(c<7)fwrite(colors[c],1,3,f);else{c-=7;unsigned char rgb[3]={(unsigned char)(c>>16),(unsigned char)(c>>8),(unsigned char)c};fwrite(rgb,1,3,f);}}fclose(f);
}
void snapshot(const char* nextSession="0123456789abcdef",const char* mode="N",int newVolume=60) {
 std::string text=std::string("B8\tS\t")+nextSession+"\tPLAYING\t"+std::to_string(newVolume)+"\t100\tSynthetic long song title Remastered 2\t-\t"+mode+"\t1\t1\t28\t61\t240\tSynthetic browser song\t-\t14285078b4dcffb4642850a0\tDemo Artist\tAll songs\t0\t"+"1"+"\t28"+(mode[0]=='N'?"\t\t\t\t":"\t\tPrevious item\tNext item\tLater item")+"\tL\tREADY\t29912288";
 std::vector<char> line(text.begin(),text.end());line.push_back(0);helloSent=true;receive(line.data());
}
void step(uint32_t at) {fakeNow=at;lastState=at;loop();M5Dial.BtnA.pressed=M5Dial.BtnA.released=false;M5Dial.Touch.detail.clicked=M5Dial.Touch.detail.pressed=M5Dial.Touch.detail.released=false;}
void touchDown(int x,int y,uint32_t at){M5Dial.Touch.detail.x=x;M5Dial.Touch.detail.y=y;M5Dial.Touch.detail.pressed=M5Dial.Touch.detail.down=true;step(at);}
void touchUp(int x,int y,uint32_t at,bool clicked=true){M5Dial.Touch.detail.x=x;M5Dial.Touch.detail.y=y;M5Dial.Touch.detail.down=false;M5Dial.Touch.detail.clicked=clicked;M5Dial.Touch.detail.released=true;step(at);}
void tap(int x,int y,uint32_t at){touchDown(x,y,at);touchUp(x,y,at+50);}
void turn(int steps,uint32_t at){M5Dial.Encoder.position+=steps*4;step(at);}
void button(bool down,uint32_t at){M5Dial.BtnA.down=down;M5Dial.BtnA.pressed=down;M5Dial.BtnA.released=!down;step(at);}

void checkClocks(const char* output) {
 Serial.plugged=Serial.usbConnected=true;Serial.freeBytes=4096;
 M5Dial.BtnA={};M5Dial.Touch.detail={};face.reset();menuTouchCaptured=false;
 suppressFaceUntil=suppressTouchUntil=0;wifi.reset();browsePending=false;
 snapshot();ui.reset();source='L';strcpy(catalogStatus,"READY");errorCode="-";playstate="PAUSED";
 fakeNow=500000;idleClock.reset(fakeNow);lastPosition=M5Dial.Encoder.read();
 step(500000);step(619999);assert(!idleClock.visible);step(620000);assert(idleClock.visible);
 auto priorClock=M5Dial.Display.pixels;
 for(int style=0;style<3;++style) {
  ui.clock=clockStyle(style);lastScreen="";draw();saveFrame(output,"clock-"+std::to_string(style));
  assert(M5Dial.Display.pixels!=priorClock);priorClock=M5Dial.Display.pixels;
  const char* expected=style==0?"10:08":style==1?"12":"SUNDAY";
  assert(std::find(M5Dial.Display.labels.begin(),M5Dial.Display.labels.end(),expected)!=M5Dial.Display.labels.end());
 }
 // Back leaves the setup observer. Cached READY must not suppress the clock
 // forever after the host intentionally stops publishing setup replies.
 ui.page=Page::Wifi;ui.selected=4;strcpy(wifi.state,"READY");ui.choose();
 step(620001);assert(ui.page==Page::Settings&&!wifi.pending);
 ui.reset();step(620002);step(740002);assert(idleClock.visible);
 // A raw, fractional encoder count wakes without changing volume. A continuing
 // spin is consumed until the knob is still for 350 ms.
 Serial.output.clear();++M5Dial.Encoder.position;step(740010);assert(!idleClock.visible);
 turn(3,740020);step(740200);assert(Serial.output.find("\tK\t")==std::string::npos);
 step(740400);turn(1,740410);assert(Serial.output.find("\tK\t1\t")!=std::string::npos);
 // Save all three settings through the real menu and NVS path, preserving visual.
 ui.mode=VisualMode::Artwork;
 for(int style=0;style<3;++style) {
  ui.page=Page::Settings;ui.selected=1;ui.choose();assert(ui.page==Page::Clock);
  ui.selected=style;ui.choose();step(fakeNow+100);
  assert(preferences.values["clock"]==style);
  assert(ui.mode==VisualMode::Artwork);
 }
 int writes=preferences.writes;step(fakeNow+100);assert(preferences.writes==writes);
 setup();assert(ui.clock==ClockStyle::Color);
 preferences.values["clock"]=255;setup();assert(ui.clock==ClockStyle::Color);
 ui.page=Page::Settings;lastScreen="";draw();saveFrame(output,"clock-settings");
 ui.selected=1;ui.choose();ui.selected=2;lastScreen="";draw();saveFrame(output,"clock-choices");
 ui.selected=3;lastScreen="";draw();saveFrame(output,"clock-choices-back");
 ui.choose();assert(ui.page==Page::Settings&&ui.clock==ClockStyle::Color);
 // A paused clock cannot interrupt menus, browsing, setup, diagnostics or a
 // button/contact already held before the timeout.
 for(int exclusion=0;exclusion<10;++exclusion) {
  ui.reset();wifi.reset();browsing=browsePending=false;playstate="PAUSED";errorCode="-";
  localMinute=29912288;source='L';songCount=1;strcpy(catalogStatus,"READY");connected=true;
  if(exclusion==0)ui.menu();if(exclusion==1)browsing=true;if(exclusion==2)browsePending=true;
  if(exclusion==3)wifi.request('S');if(exclusion==4)errorCode="NETWORK";
  if(exclusion==5)strcpy(catalogStatus,"OFFLINE");if(exclusion==6)localMinute=0;
  if(exclusion==7)M5Dial.BtnA.down=true;if(exclusion==8)M5Dial.Touch.detail.down=true;
  if(exclusion==9){source='S';songCount=0;}
  idleClock.reset(fakeNow);if(exclusion==2)browsePendingAt=fakeNow+120001;step(fakeNow+120001);
  if(exclusion==2)browsePendingAt=fakeNow+120001;step(fakeNow+120001);assert(!idleClock.visible);
  M5Dial.BtnA={};M5Dial.Touch.detail={};face.reset();menuTouchCaptured=false;
 }
 ui.reset();wifi.reset();browsing=browsePending=false;playstate="PAUSED";errorCode="-";
 localMinute=29912288;source='L';songCount=1;strcpy(catalogStatus,"READY");connected=true;
 idleClock.reset(fakeNow);step(fakeNow+1);step(fakeNow+120000);assert(idleClock.visible);
 // Physical face touches can precede the button. Capture the whole contact,
 // including a hold, touch click and button release, without play/skip/browse.
 Serial.output.clear();uint32_t wakeAt=fakeNow+10;
 touchDown(167,190,wakeAt);button(true,wakeAt+10);step(wakeAt+1000);
 touchUp(167,190,wakeAt+1010);button(false,wakeAt+1020);step(wakeAt+1400);
 assert(Serial.output.find("\tP\t")==std::string::npos);
 assert(Serial.output.find("\tS\t")==std::string::npos);
 assert(Serial.output.find("\tL\t")==std::string::npos);
 button(true,wakeAt+1500);button(false,wakeAt+1550);
 assert(Serial.output.find("\tP\t1\t")!=std::string::npos);
 // Long playback followed by pause gets a fresh interval.
 playstate="PLAYING";step(fakeNow+500000);assert(!idleClock.visible);
 playstate="PAUSED";step(fakeNow+10);assert(!idleClock.visible);
 step(fakeNow+119999);assert(!idleClock.visible);step(fakeNow+1);assert(idleClock.visible);
 auto pixels=M5Dial.Display.pixels;int draws=M5Dial.Display.drawCount;
 step(fakeNow+100);assert(M5Dial.Display.drawCount==draws);assert(pixels==M5Dial.Display.pixels);
 ++localMinute;step(fakeNow+100);assert(M5Dial.Display.pixels!=pixels);
 // Invalid clock records cannot partially commit a title, session or heartbeat.
 std::string valid=std::string("B8\tS\t")+session+"\tPAUSED\t60\t100\tRejected title\t-\tN\t1\t0\t1\t0\t0\tSynthetic\t-\t000000000000000000000000\tDemo Artist\tAll songs\t0\t1\t1\t\t\t\t\tL\tREADY\t";
 for(const char* bad:{"1","-1","68374080","NaN","29912288\textra"}) {
  std::string line=valid+bad;std::vector<char> bytes(line.begin(),line.end());bytes.push_back(0);
  long oldMinute=localMinute;uint32_t oldState=lastState;std::string oldTitle=title;
  receive(bytes.data());assert(oldMinute==localMinute&&oldState==lastState&&oldTitle==title);
 }
 // A zero timestamp explicitly withdraws trust. Session and USB loss erase it.
 {std::string line=valid+"0";std::vector<char> bytes(line.begin(),line.end());bytes.push_back(0);receive(bytes.data());step(fakeNow+10);assert(!idleClock.visible&&localMinute==0);}
 snapshot("eeeeeeeeeeeeeeee");assert(!idleClock.visible);
 playstate="PAUSED";step(fakeNow+1);step(fakeNow+120000);assert(idleClock.visible);
 fakeNow+=3000;loop();assert(!connected&&!idleClock.visible&&localMinute==0);
 saveFrame(output,"clock-disconnected");
}

int main(int argc,char** argv){
 (void)argc; setup();snapshot();
 uint8_t synthetic[12]={20,40,80,120,180,220,255,180,100,40,80,160};
 for(int state=0;state<17;++state){
  fakeNow=state*100;lastState=fakeNow;
  connected=state!=3 && state!=12;browsing=state==1 || state==13;browseMode=state==13?'A':browsing?'B':'N';browseCount=28;playstate=state==2?"ERROR":state==4||state==8||state==16?"PAUSED":"PLAYING";
  ui.reset();ui.mode=state>=7?VisualMode::Colorful:VisualMode::Artwork;shuffleEnabled=state==6||state==15||state==16;
  if(state==9)ui.menu();if(state==10)ui.page=Page::Display;if(state==11)ui.page=Page::Settings;if(state==14){ui.menu();ui.selected=5;}if(state==15){ui.menu();shuffleEnabled=true;}if(state==16){ui.reset();shuffleEnabled=true;}
  artwork.ready=state==0 || state==4 || state==6;
  for(int i=0;i<112*112;++i) artwork.pixels[i]=(i%112<56)?0x00f8:0xe007;
  memcpy(levels,synthetic,sizeof(levels));if(state==8)memset(levels,0,sizeof(levels));
  elapsed=state==6?999999:61; volume=state==6?100:60;
  errorCode=state==2?"SOURCE":"-";
  strcpy(title,"Synthetic long song title with a distinct Remastered 2 suffix");
  strcpy(cursorTitle,state==13?"Synthetic Artist":title);if(state==13)browseCount=4;
  if(browsing){strcpy(neighborTitles[0],"Earlier song");strcpy(neighborTitles[1],"Previous song");strcpy(neighborTitles[2],"Next song");strcpy(neighborTitles[3],"Later song");}lastScreen="";draw();
  auto& d=M5Dial.Display;
  if(connected && artwork.ready && !browsing && !ui.open() && ui.mode==VisualMode::Artwork){
   assert(d.pixels[80*240+80] == 7+0xff0000);assert(d.pixels[80*240+150] == 7+0x00ff00);
  }
  if(!browsing&&!ui.open()) {
    for(auto label:d.labels)assert(label!="LOCAL" && label!="MENU" && label!="< PREV" && label!="NEXT >" && label!="PLAYING" && label!="PAUSED" && label!="STOPPED");
    for(int y:d.labelY)assert(y==24 || y==212);
    if(connected&&playstate!="ERROR")assert(d.pixels[213*240+57]==encodeColor(WHITE)); // Local source icon.
    if(state==16){assert(d.pixels[212*240+91]==encodeColor(WHITE));assert(d.pixels[212*240+96]==BLACK);} // Pause bars remain distinct.
    int shufflePixels=0;for(int y=205;y<=219;++y)for(int x=164;x<=182;++x)shufflePixels+=d.pixels[y*240+x]!=BLACK;
    if(connected&&playstate!="ERROR")assert(shuffleEnabled?shufflePixels>0:shufflePixels==0);
    if(!connected){bool diagnostic=false;for(auto label:d.labels)diagnostic=diagnostic||label=="DISCONNECTED";assert(diagnostic);}
  }
  saveFrame(argv[1],"state-"+std::to_string(state));
 }
 // The real renderer must move the title even when no host state changes.
 ui.reset();ui.mode=VisualMode::Artwork;connected=true;browsing=false;elapsed=61;volume=60;playstate="PAUSED";
 fakeNow=2000;lastState=2000;lastScreen="";draw();
 auto first=M5Dial.Display.pixels;
 fakeNow=4000;draw();assert(first!=M5Dial.Display.pixels);
 int width=M5Dial.Display.textWidth(title);
 fakeNow=2000+1400+(width-112)*50;draw();saveFrame(argv[1],"title-end");
 for(int y=16;y<32;++y)for(int x=0;x<240;++x)if(x<64||x>=176)assert(M5Dial.Display.pixels[y*240+x]==BLACK);
 // Short labels remain centered and never animate.
 strcpy(title,"Short song");fakeNow+=100;draw();first=M5Dial.Display.pixels;fakeNow+=2000;draw();assert(first==M5Dial.Display.pixels);
 // Quiet Colorful frames remain identical across time and ignore stale levels.
 ui.mode=VisualMode::Colorful;lastScreen="";draw();first=M5Dial.Display.pixels;
 memset(levels,255,sizeof(levels));fakeNow+=100;draw();assert(first==M5Dial.Display.pixels);
 fakeNow+=2000;draw();assert(first==M5Dial.Display.pixels);
 // Menu opens at contact, even when M5 later rejects the release as a hold/flick.
 // Playback and menu choices still require a release-click.
 for(char playerSource:{'L','S'}) for(const char* state:{"PLAYING","BUFFERING","PAUSED"}) {
  snapshot();source=playerSource;playstate=state;ui.reset();Serial.output.clear();
  touchDown(120,230,29000);assert(ui.page==Page::Menu);
  step(29500);assert(ui.page==Page::Menu); // No click event after a hold/flick.
  touchUp(120,82,29550,false);assert(ui.page==Page::Menu);assert(!menuTouchCaptured);
  for(const char* action:{"\tP\t","\tS\t","\tL\t","\tK\t"})assert(Serial.output.find(action)==std::string::npos);
  touchDown(120,82,29600);assert(ui.page==Page::Menu);assert(ui.pending==MenuAction::None);touchUp(120,82,29650,false);
  ui.reset();Serial.output.clear();touchDown(60,190,29700);assert(!ui.open());assert(Serial.output.find("\tS\t")==std::string::npos);touchUp(60,190,29750,false);
 }
 // Even a click-classified release over a row belongs to the opening contact.
 for(int releaseY:{230,222,82}) {
  snapshot();ui.reset();Serial.output.clear();touchDown(120,230,29700);
  touchUp(120,releaseY,29750);assert(ui.page==Page::Menu);assert(ui.pending==MenuAction::None);
  assert(Serial.output.find("\tL\t")==std::string::npos);
 }
 snapshot();ui.reset();browsePending=true;browsePendingAt=29800;touchDown(120,230,29800);assert(!ui.open());browsePending=false;
 touchUp(120,230,29820,false);
 snapshot("0123456789abcdef","B");touchDown(120,230,29850);assert(!ui.open());
 touchUp(120,230,29870,false);
 snapshot();connected=false;touchDown(120,230,29900);assert(!ui.open());
 touchUp(120,230,29920,false);
 snapshot();M5Dial.BtnA.down=true;touchDown(120,230,29930);assert(!ui.open());
 touchUp(120,230,29940,false);M5Dial.BtnA.down=false;suppressTouchUntil=0;
 // Actual loop routing: menu touches/turns/presses must not change playback or volume.
 snapshot(); ui.mode=VisualMode::Artwork; Serial.output.clear();
 tap(120,230,30000);assert(ui.page==Page::Menu);
 turn(5,30400);assert(ui.selected==5);
 button(true,30800);button(false,30850);assert(ui.page==Page::Settings);
 tap(120,146,31300);assert(ui.page==Page::Menu);
 turn(4,31400);assert(ui.selected==4);
 button(true,31700);button(false,31750);assert(ui.page==Page::Display);
 tap(120,114,32150);assert(ui.mode==VisualMode::Colorful&&!ui.open());assert(preferences.values["visual"]==1);
 assert(Serial.output.find("\tK\t")==std::string::npos);assert(Serial.output.find("\tP\t")==std::string::npos);assert(Serial.output.find("\tS\t")==std::string::npos);assert(Serial.output.find("\tL\t")==std::string::npos);
 Serial.output.clear();turn(1,32500);assert(Serial.output.find("\tK\t1\tN")!=std::string::npos);
 Serial.output.clear();button(true,32900);button(false,32950);assert(Serial.output.find("\tP\t0\tN")!=std::string::npos);
 Serial.output.clear();button(true,33400);step(34000);button(false,34100);assert(Serial.output.find("\tL\t0\tN")!=std::string::npos);assert(Serial.output.find("\tP\t")==std::string::npos);
 snapshot("0123456789abcdef","B");ui.reset();Serial.output.clear();turn(1,34500);assert(Serial.output.find("\tK\t1\tB")!=std::string::npos);
 // Reconnect and watchdog discard local navigation and armed gestures.
 ui.menu();face.press(34500,true);snapshot("fedcba9876543210");assert(!ui.open()&&!face.armed);
 ui.menu();fakeNow=38000;lastState=34500;loop();assert(!connected&&!ui.open()&&!face.armed);
 assert(ui.mode==VisualMode::Colorful);
 // A browser cannot remain apparently usable after its host stops sending snapshots.
 for(const char* mode:{"B","A"}) {
  fakeNow=38000;snapshot("fedcba9876543210",mode);assert(connected&&browsing);
  fakeNow+=3000;loop();assert(!connected&&!browsing&&browseMode=='N');
  bool diagnostic=false;for(auto text:M5Dial.Display.labels)diagnostic=diagnostic||text=="DISCONNECTED";
  assert(diagnostic);saveFrame(argv[1],std::string("wheel-disconnected-")+mode);
 }
 // Playback faults remain visible while the list stays available for recovery.
 fakeNow=38000;
 for(const char* mode:{"B","A"}) for(const char* error:{"SOURCE","STARTUP","OUTPUT","PLAYER"}) {
  fakeNow+=100;snapshot("fedcba9876543210",mode);std::string selected=cursorTitle;
  playstate="ERROR";errorCode=error;draw();
  bool diagnostic=false;for(auto text:M5Dial.Display.labels)diagnostic=diagnostic||text==error;
  assert(diagnostic&&browsing&&selected==cursorTitle);
  saveFrame(argv[1],std::string("wheel-error-")+mode+"-"+error);
 }
 snapshot();Serial.output.clear();
 tap(60,190,39000);assert(Serial.output.find("\tS\t-1\tN")!=std::string::npos);
 Serial.output.clear();tap(120,230,39400);assert(ui.open());
 button(true,39800);step(40400);button(false,40500);assert(!ui.open());
 assert(Serial.output.find("\tL\t")==std::string::npos);assert(Serial.output.find("\tP\t")==std::string::npos);
 // Explicit catalog and shuffle actions go to host once, using its confirmed state.
 snapshot();tap(120,230,41000);Serial.output.clear();tap(120,82,41400);assert(Serial.output.find("\tL\t0\tN\t1")!=std::string::npos);assert(browsePending);
 snapshot();browsePending=false;tap(120,230,41800);Serial.output.clear();tap(120,114,42200);assert(Serial.output.find("\tL\t1\tN\t1")!=std::string::npos);
 snapshot();browsePending=false;tap(120,230,42600);Serial.output.clear();turn(3,42800);button(true,43000);button(false,43050);assert(Serial.output.find("\tQ\t1\tN\t1")!=std::string::npos);assert(!shuffleEnabled);
 ui.menu();lastScreen="";draw();int drawn=M5Dial.Display.drawCount;fakeNow+=2500;draw();assert(M5Dial.Display.drawCount==drawn);
 // Host confirms artist browser; hold exits using the shown token and clears pending on N.
 ui.menu();snapshot("0123456789abcdef","A");assert(!ui.open());Serial.output.clear();button(true,46000);step(46600);button(false,46700);
 assert(Serial.output.find("\tL\t-1\tA\t1")!=std::string::npos);assert(browsePending);
 snapshot();assert(!browsePending&&!browsing);
 // Exercise actual receive()/Serial ACK path, including a lost progress/final ACK.
 ui.reset();artwork.bind(session,1,"e2ce1e0a"); // CRC32 of the complete zero image.
 auto chunk=[&](size_t offset,size_t length){return String("B8\tA\t")+session+"\t1\te2ce1e0a\t"+String((int)offset)+"\t"+String(std::string(length*2,'0'))+"\n";};
 auto deliver=[&](String line){Serial.input=line;Serial.output.clear();step(47000);};
 deliver(chunk(0,512));assert(artwork.used==512);assert(Serial.output.find("\tO\t")!=std::string::npos);assert(Serial.output.find("\t512\n")!=std::string::npos);
 deliver(chunk(0,512));assert(artwork.used==512);assert(Serial.output.find("\t512\n")!=std::string::npos);
 deliver(chunk(1024,512));assert(artwork.used==512);assert(Serial.output.find("\t512\n")!=std::string::npos);
 deliver(chunk(512,0));assert(artwork.used==512);assert(Serial.output.find("\tO\t")==std::string::npos);
 size_t last=0,lastLength=0;
 for(size_t offset=512;offset<Artwork::BYTES;offset+=512){size_t length=std::min((size_t)512,Artwork::BYTES-offset);last=offset;lastLength=length;deliver(chunk(offset,length));}
 assert(artwork.ready);assert(Serial.output.find("B8\tC\t")!=std::string::npos);
 deliver(chunk(last,lastLength));assert(artwork.ready);assert(Serial.output.find("B8\tC\t")!=std::string::npos);
 // Curved rails span empty, half and full progress without touching transport.
 snapshot();ui.reset();ui.mode=VisualMode::Artwork;playstate="PAUSED";strcpy(title,"Short song");duration=240;shuffleEnabled=false;
 int previousProgress=-1,previousVolume=-1;
 for(int value=0;value<=100;value+=50){
   fakeNow=60000+value*100;volume=value;elapsed=value*duration/100;volumeIndicator.changed(fakeNow);lastScreen="";draw();
   int progressPixels=0,volumePixels=0;auto& d=M5Dial.Display;
   for(int y=40;y<=174;++y)for(int x=0;x<240;++x)if(d.pixels[y*240+x]==encodeColor(CYAN)){if(x>=190)++progressPixels;if(x<=45)++volumePixels;}
   if(value==0)assert(progressPixels==0 && volumePixels==0);
   else assert(progressPixels>previousProgress && volumePixels>previousVolume);
   previousProgress=progressPixels;previousVolume=volumePixels;
   saveFrame(argv[1],"arc-"+std::to_string(value));
 }
 // Same-session host changes alone trigger volume. Startup/reconnect and unchanged snapshots do not.
 fakeNow=80000;snapshot("1111111111111111");assert(volumeIndicator.brightness(fakeNow)==0);
 fakeNow=80100;snapshot("1111111111111111","N",50);assert(volumeIndicator.brightness(fakeNow)==255);
 uint32_t changed=volumeIndicator.changedAt;
 fakeNow=81500;snapshot("1111111111111111","N",50);assert(volumeIndicator.changedAt==changed);
 strcpy(title,"Short song");ui.mode=VisualMode::Artwork;lastScreen="";draw();first=M5Dial.Display.pixels;saveFrame(argv[1],"volume-held");
 fakeNow=82100;draw();assert(M5Dial.Display.pixels!=first);saveFrame(argv[1],"volume-fading");first=M5Dial.Display.pixels;
 fakeNow=82400;draw();assert(volumeIndicator.brightness(fakeNow)==0);assert(M5Dial.Display.pixels!=first);
 for(int y=40;y<=174;++y)for(int x=0;x<=45;++x)assert(M5Dial.Display.pixels[y*240+x]==BLACK);
 saveFrame(argv[1],"volume-hidden");
 fakeNow=82500;snapshot("2222222222222222","N",100);assert(volumeIndicator.brightness(fakeNow)==0);
 // At the volume ceiling, an unchanged host snapshot does not create a phantom overlay.
 turn(1,83000);snapshot("2222222222222222","N",100);assert(volumeIndicator.brightness(fakeNow)==0);
 // Audio drives expressive geometry. Empty/paused histories remain still, including the center.
 ui.mode=VisualMode::Colorful;strcpy(title,"Short song");playstate="PLAYING";memset(levels,255,sizeof(levels));fakeNow=84000;lastScreen="";draw();
 saveFrame(argv[1],"colorful-full");first=M5Dial.Display.pixels;
 memset(levels,70,sizeof(levels));fakeNow+=100;draw();assert(first!=M5Dial.Display.pixels);saveFrame(argv[1],"colorful-soft");
 memset(levels,0,sizeof(levels));fakeNow+=100;draw();first=M5Dial.Display.pixels;fakeNow+=1000;draw();assert(first==M5Dial.Display.pixels);
 playstate="PAUSED";fakeNow+=100;draw();first=M5Dial.Display.pixels;memset(levels,255,sizeof(levels));fakeNow+=100;draw();assert(first==M5Dial.Display.pixels);
 // Wi-Fi settings renders only synthetic credentials through the actual parser.
 snapshot();ui.page=Page::Settings;ui.selected=0;lastScreen="";draw();saveFrame(argv[1],"wifi-settings");
 ui.choose();assert(ui.page==Page::WifiConfirm&&ui.selected==0);ui.pending=MenuAction::None;
 lastScreen="";draw();saveFrame(argv[1],"wifi-confirm");
 wifi.reset();wifi.request('S');ui.page=Page::Wifi;ui.selected=0;
 {char incoming[]="B8\tW\t0123456789abcdef\t1\t11111111111111111111111111111111\t1\tREADY\t600\t46616d696c792d526164696f2d44454d4f\t73796e7468657469632d6f6e6c792d32342d706173736b6579\t574946493a543a5750413b533a46616d696c792d526164696f2d44454d4f3b503a73796e7468657469632d6f6e6c792d32342d706173736b65793b3b\t-";receive(incoming);assert(!incoming[0]);}
 assert(wifi.hasAccess());assert(std::string(wifi.ssid)=="Family-Radio-DEMO");
 // Cached replies must not postpone a new status poll or extend credentials.
 {
  auto hex=[](const char* value){std::string out;const char* digits="0123456789abcdef";for(;*value;++value){unsigned char c=*value;out+=digits[c>>4];out+=digits[c&15];}return out;};
  std::vector<std::string> values={"B8","W",session,std::to_string(wifi.requestId),wifi.setupId,
    std::to_string(wifi.revision),wifi.state,std::to_string(wifi.remaining),hex(wifi.ssid),hex(wifi.password),hex(wifi.payload),wifi.error};
  char* fields[12];for(int i=0;i<12;++i)fields[i]=&values[i][0];
  uint32_t accepted=wifi.receivedAt;
  for(int i=1;i<=3;++i){assert(!wifi.receive(fields,accepted+i*300));assert(wifi.receivedAt==accepted);}
  step(accepted+1001);assert(wifi.pending && wifi.command=='Q');
 }
 lastScreen="";draw();saveFrame(argv[1],"wifi-join-qr");
 assert(lastScreen.find(wifi.password)==std::string::npos);
 ui.selected=1;lastScreen="";draw();saveFrame(argv[1],"wifi-portal-qr");
 ui.selected=2;lastScreen="";draw();saveFrame(argv[1],"wifi-details");
 ui.selected=3;lastScreen="";draw();saveFrame(argv[1],"wifi-cancel");
 // Leaving the panel cannot replace a Cancel that has not been acknowledged.
 WifiUI beforeCancel=wifi;
 wifi.request('T');ui.selected=4;ui.choose();step(fakeNow+10);
 assert(ui.page==Page::Wifi && wifi.command=='T' && wifi.pending);
 {
  std::string reply=std::string("B8\tW\t")+session+"\t"+std::to_string(wifi.requestId)+"\t"+wifi.setupId+"\t2\tSTOPPING\t0\t-\t-\t-\t-";
  std::vector<char> line(reply.begin(),reply.end());line.push_back(0);receive(line.data());assert(!wifi.pending);
 }
 wifi=beforeCancel;
 ui.selected=4;ui.choose();assert(ui.page==Page::Settings&&ui.pending==MenuAction::WifiExit);
 // A delayed reply from a previous request cannot replace the visible session.
 {char stale[]="B8\tW\t0123456789abcdef\t0\t11111111111111111111111111111111\t1\tCONNECTED\t0\t-\t-\t-\t-";receive(stale);assert(!stale[0]);}
 assert(wifi.hasAccess());
 // Leaving the panel erases both decoded secrets and a partial raw credential record.
 ui.pending=MenuAction::WifiExit;strcpy(input,"B8\tW\tpartial-secret");used=strlen(input);dropping=false;loop();
 assert(!wifi.hasAccess()&&!wifi.password[0]&&!input[0]&&used==0&&dropping);
 // Startup requires an explicit second choice; Back is the safe default.
 ui.page=Page::WifiConfirm;ui.selected=0;ui.choose();assert(ui.page==Page::Settings);
 ui.page=Page::WifiConfirm;ui.selected=1;ui.choose();assert(ui.pending==MenuAction::WifiStart);
 ui.pending=MenuAction::None;wifi.reset();ui.reset();snapshot();
 // Parse real state records before rendering progress, retry and terminal screens.
 {
  auto hex=[](const char* value){std::string out;const char* digits="0123456789abcdef";for(;*value;++value){unsigned char c=*value;out+=digits[c>>4];out+=digits[c&15];}return out;};
  auto state=[&](const char* value,int revision,bool access,const char* expected,Page page,const char* frame){
    std::string reply=std::string("B8\tW\t")+session+"\t"+std::to_string(wifi.requestId)+"\t11111111111111111111111111111111\t"+std::to_string(revision)+"\t"+value+"\t"+((!strcmp(value,"CONNECTED")||!strcmp(value,"EXPIRED"))?"0":"580")+"\t"+
      (access?hex("Family-Radio-DEMO"):"-")+"\t"+(access?hex("synthetic-only-24-passkey"):"-")+"\t"+
      (access?hex("WIFI:T:WPA;S:Family-Radio-DEMO;P:synthetic-only-24-passkey;;"):"-")+"\t"+(!strcmp(value,"FAILED")?"NETWORK":"-");
    std::vector<char> line(reply.begin(),reply.end());line.push_back(0);fakeNow+=100;receive(line.data());
    assert(!line[0]&&ui.page==page&&wifi.hasAccess()==access);
    if(!access)assert(!wifi.password[0]&&!wifi.payload[0]);
    ui.selected=0;lastScreen="";draw();
    assert(std::find(M5Dial.Display.labels.begin(),M5Dial.Display.labels.end(),expected)!=M5Dial.Display.labels.end());
    saveFrame(argv[1],frame);
  };
  wifi.request('S');ui.page=Page::Wifi;
  state("STARTING",1,false,"Starting setup",Page::Wifi,"wifi-starting");
  wifi.request('Q');state("CONNECTING",2,false,"Joining network",Page::Wifi,"wifi-joining");
  wifi.request('Q');state("FAILED",3,true,"TRY AGAIN",Page::Wifi,"wifi-retry-qr");
  wifi.request('T');state("STOPPING",4,false,"Closing setup",Page::Wifi,"wifi-stopping");
  // A durable join may win the race with cancellation.
  wifi.request('Q');state("CONNECTED",5,false,"Wi-Fi saved",Page::WifiResult,"wifi-connected");
  wifi.reset();wifi.request('P');ui.page=Page::WifiConfirm;
  state("EXPIRED",6,false,"Setup timed out",Page::WifiResult,"wifi-expired");
 }
 wifi.reset();ui.reset();snapshot();
 // A resumed host cannot drain a full HWCDC ring until its TX path is
 // re-armed. Polling capacity alone must not strand the reconnect handshake.
 connected=false;helloSent=false;lastHello=0;Serial.output.clear();
 Serial.usbConnected=false;Serial.freeBytes=0;fakeNow=200000;
 for(int attempt=0;attempt<3;++attempt){loop();Serial.hostPickup();fakeNow+=1001;}
 assert(Serial.output.find("B8\tREADY\n")!=std::string::npos);
 // HWCDC can retain a stale connected flag while IN_EMPTY is disarmed.
 // Once the protocol times out, stale TX bytes must not block a fresh READY.
 connected=false;helloSent=false;Serial.output.clear();Serial.pending="stale";
 Serial.usbConnected=true;Serial.freeBytes=0;Serial.txArmed=false;
 for(int attempt=0;attempt<4;++attempt){fakeNow+=1001;loop();Serial.hostPickup();}
 assert(Serial.output.find("B8\tREADY\n")!=std::string::npos);
 assert(Serial.output.find("stale")==std::string::npos);
 // A failed READY enqueue does not authorize an incoming host snapshot.
 helloSent=false;Serial.output.clear();Serial.nextWriteLimit=0;fakeNow+=1001;loop();assert(!helloSent);
 fakeNow+=1001;loop();assert(helloSent);
 // Readiness probes also cover events and art ACKs. A gesture dropped during
 // backpressure must never be queued and replayed after the host returns.
 snapshot();Serial.output.clear();Serial.usbConnected=false;Serial.freeBytes=0;Serial.txArmed=false;
 event('P',0);assert(Serial.txArmed);Serial.hostPickup();event('H',0);
 assert(Serial.output.find("\tH\t")!=std::string::npos);assert(Serial.output.find("\tP\t")==std::string::npos);
 artwork.bind(session,1,"e2ce1e0a");Serial.output.clear();Serial.usbConnected=false;Serial.freeBytes=0;Serial.txArmed=false;
 deliver(chunk(0,2));assert(Serial.txArmed);Serial.hostPickup();deliver(chunk(0,2));
 assert(Serial.output.find("\tO\t")!=std::string::npos);assert(artwork.used==2);
 Serial.output.clear();Serial.plugged=false;Serial.usbConnected=false;Serial.freeBytes=4096;Serial.txArmed=false;
 event('P',0);assert(Serial.output.empty());assert(!Serial.txArmed);
 assert(Serial.pending.empty());
 connected=false;helloSent=false;fakeNow+=1001;loop();assert(!helloSent);assert(Serial.output.empty());
 Serial.plugged=true;Serial.usbConnected=true;Serial.freeBytes=0;int writes=Serial.writeCalls;
 Serial.hostPickup();assert(Serial.output.find("\tP\t")==std::string::npos);
 connected=true;
 event('P',0);assert(Serial.writeCalls==writes);assert(Serial.output.empty());
 checkClocks(argv[1]);
}

'''
sys.path.insert(0,str(Path.cwd()))
from host.radio import Controller
class SyntheticPlayer:
    def pause(self, value): pass
class Sink:
    def __call__(self, value): pass
wheel_checks=[]
for kind in ('B','A'):
    for count in (1,2,1000,20000):
        songs=[dict(id=str(i),title=f'Synthetic song {i:04}',path=f'{i}.wav',artist=f'Artist {i:04}') for i in range(count)]
        c=Controller(SyntheticPlayer(),Sink(),songs=songs)
        c.receive('B8\tREADY',0);c.session='3333333333333333';c.open_browser(kind,0)
        for cursor in sorted({0,count//2,count-1}):
            c.cursor=cursor
            record=c.snapshot().decode().rstrip('\n')
            wheel_checks.append((f'wheel-{kind}-{count}-{cursor}',record))
songs=[dict(id=str(i),title=f'Filtered song {i}',path=f'{i}.wav',artist='Demo Artist' if i%2 else 'Other') for i in range(4)]
c=Controller(SyntheticPlayer(),Sink(),songs=songs);c.receive('B8\tREADY',0);c.session='3333333333333333';c.open_browser('B',0,'Demo Artist');c.cursor=1
wheel_checks.append(('wheel-filtered',c.snapshot().decode().rstrip('\n')))
for song in songs: song['artist']='Demo Artist'
c=Controller(SyntheticPlayer(),Sink(),songs=songs);c.receive('B8\tREADY',0);c.session='3333333333333333';c.open_browser('A',0)
wheel_checks.append(('wheel-demo-artist',c.snapshot().decode().rstrip('\n')))
for song in songs: song['title']='A long selected synthetic song title with a different ending '+song['id']
c=Controller(SyntheticPlayer(),Sink(),songs=songs);c.receive('B8\tREADY',0);c.session='3333333333333333';c.open_browser('B',0);c.cursor=1
wheel_checks.append(('wheel-long',c.snapshot().decode().rstrip('\n')))
songs=[dict(id=str(i),title='W'*64,path=f'{i}.wav',artist='A'*64) for i in range(1000)]
c=Controller(SyntheticPlayer(),Sink(),songs=songs);c.receive('B8\tREADY',0);c.session='3333333333333333';c.current=999;c.open_browser('B',0,'A'*64);c.cursor=997
c.elapsed=c.duration=999999;c.error='STARTUP';c.volume=100;c.browser_token=2147483647
class SyntheticAssets:
    def get(self, song): return bytes(25088),bytes([255])*20,'ffffffff'
c.visuals=SyntheticAssets()
maximum_snapshot=c.snapshot().decode().rstrip('\n')
assert len(maximum_snapshot)+1<=800
wheel_checks.append(('wheel-max',maximum_snapshot))
wheel_cpp=''
for name,record in wheel_checks:
    fields=record.split('\t')
    wheel_cpp+='{char record[]='+json.dumps(record)+';fakeNow+=100;helloSent=true;receive(record);assert(browsing&&!ui.open());lastScreen="";draw();saveFrame(argv[1],'+json.dumps(name)+');'
    wheel_cpp+='assert(std::string(cursorTitle)=='+json.dumps(fields[14])+');'
    for i in range(4):wheel_cpp+='assert(std::string(neighborTitles['+str(i)+'])=='+json.dumps(fields[22+i])+');'
    if name=='wheel-long':
        wheel_cpp+='auto before=M5Dial.Display.pixels;fakeNow+=2000;draw();assert(before!=M5Dial.Display.pixels);'
        wheel_cpp+='for(int y=0;y<240;++y)if(y<100||y>=128)for(int x=0;x<240;++x)assert(before[y*240+x]==M5Dial.Display.pixels[y*240+x]);'
        wheel_cpp+='saveFrame(argv[1],"wheel-long-scrolled");'
    wheel_cpp+='Serial.output.clear();tap(167,190,fakeNow+400);tap(120,86,fakeNow+400);assert(Serial.output.find("\\tS\\t")==std::string::npos);assert(Serial.output.find("\\tP\\t")==std::string::npos);'
    wheel_cpp+='Serial.output.clear();button(true,fakeNow+400);button(false,fakeNow+50);std::string expected="\\tP\\t"+std::to_string(cursorIndex)+"\\t"+browseMode+"\\t"+std::to_string(browserToken);assert(Serial.output.find(expected)!=std::string::npos);}'

# Malformed neighbor windows must leave the complete previously accepted state intact.
invalid_windows=[]
fields=wheel_checks[2][1].split('\t')  # Two-song browser at its final item.
for slot,value in [(23,''),(24,'Outside list'),(23,'X'*65),(23,'Bad\x7fcharacter')]:
    invalid=fields.copy();invalid[slot]=value;invalid[14]='Must not commit'
    invalid_windows.append('\t'.join(invalid))
fields=maximum_snapshot.split('\t');fields[8]='N';fields[20]='1';fields[21]='1000';fields[22:26]=['Unexpected','','',''];fields[14]='Must not commit'
invalid_windows.append('\t'.join(fields))
for record in invalid_windows:
    wheel_cpp+='{std::string oldTitle=cursorTitle,oldSession=session;std::vector<std::string> oldNeighbors;for(int i=0;i<4;++i)oldNeighbors.push_back(neighborTitles[i]);'
    wheel_cpp+='bool oldBrowsing=browsing;uint32_t oldLastState=lastState;char invalid[]='+json.dumps(record)+';fakeNow+=100;receive(invalid);'
    wheel_cpp+='assert(oldTitle==cursorTitle&&oldSession==session&&oldBrowsing==browsing&&oldLastState==lastState);for(int i=0;i<4;++i)assert(oldNeighbors[i]==neighborTitles[i]);}'
main=main.replace(' // Curved rails span empty, half and full progress without touching transport.',wheel_cpp+'\n // Curved rails span empty, half and full progress without touching transport.')
fixture=json.loads(Path('docs/fixtures/protocol-b8.json').read_text())
fixture_check='char fixture[]='+json.dumps(fixture['snapshot'].rstrip('\n'))+'; helloSent=true; receive(fixture); assert(connected && !browsing && ceiling==100 && !shuffleEnabled);'
main=main.replace('(void)argc; setup();snapshot();','(void)argc; setup();'+fixture_check+'snapshot();')
source_checks=[]
base=fixture['snapshot'].rstrip('\n').split('\t')
for status in ('LOADING','EMPTY','OFFLINE','AUTH','ERROR'):
    fields=base.copy();fields[6]='No songs';fields[8]='P';fields[9]=fields[10]=fields[11]=fields[21]='0';fields[14]='No playlists';fields[26]='S';fields[27]=status
    source_checks.append(('spotify-empty-'+status,'\t'.join(fields)))
for mode,label in [('R','Local songs'),('P','Synthetic approved playlist'),('A','Synthetic approved artist'),('B','Synthetic approved song')]:
    fields=base.copy();fields[6]='Synthetic Spotify song';fields[8]=mode;fields[14]=label;fields[17]='Synthetic approved artist';fields[26]='S'
    fields[21]='2' if mode=='R' else '1';fields[24]='Spotify' if mode=='R' else ''
    source_checks.append(('spotify-browser-'+mode,'\t'.join(fields)))
source_cpp=''
for name,record in source_checks:
    source_cpp+='{char incoming[]='+json.dumps(record)+';fakeNow+=100;helloSent=true;receive(incoming);assert(connected&&browsing&&source==\'S\');lastScreen="";draw();saveFrame(argv[1],'+json.dumps(name)+');'
    if 'empty' in name:
        source_cpp+='assert(browseCount==0);Serial.output.clear();button(true,fakeNow+400);button(false,fakeNow+50);assert(Serial.output.find("\\tP\\t0\\t")==std::string::npos);'
    source_cpp+='}'
source_cpp+='snapshot();source=\'S\';ui.spotify=true;ui.mode=VisualMode::Colorful;artwork.ready=true;strcpy(title,"Synthetic Spotify song");strcpy(currentArtist,"Synthetic approved artist");'
source_cpp+='for(int i=0;i<112*112;++i)artwork.pixels[i]=0x00f8;memset(levels,255,sizeof(levels));lastScreen="";draw();assert(ui.mode==VisualMode::Colorful);assert(M5Dial.Display.pixels[80*240+80]==7+0xff0000);saveFrame(argv[1],"spotify-artwork");'
source_cpp+='for(int row=0;row<8;++row){ui.menu();ui.spotify=true;ui.selected=row;lastScreen="";draw();assert(ui.count()==8);saveFrame(argv[1],"spotify-menu-"+std::to_string(row));}'
source_cpp+='ui.menu();ui.spotify=true;ui.selected=5;lastScreen="";draw();assert(std::find(M5Dial.Display.labels.begin(),M5Dial.Display.labels.end(),"Local display")!=M5Dial.Display.labels.end());ui.choose();lastScreen="";draw();assert(std::find(M5Dial.Display.labels.begin(),M5Dial.Display.labels.end(),"LOCAL DISPLAY")!=M5Dial.Display.labels.end());saveFrame(argv[1],"spotify-local-display");'
source_cpp+='ui.reset();source=\'L\';ui.spotify=false;'
main=main.replace(' playstate="PAUSED";fakeNow+=100;draw();first=M5Dial.Display.pixels;memset(levels,255,sizeof(levels));fakeNow+=100;draw();assert(first==M5Dial.Display.pixels);', ' playstate="PAUSED";fakeNow+=100;draw();first=M5Dial.Display.pixels;memset(levels,255,sizeof(levels));fakeNow+=100;draw();assert(first==M5Dial.Display.pixels);\n'+source_cpp)

with tempfile.TemporaryDirectory() as root:
    source=Path(root)/'draw.cpp'; binary=Path(root)/'draw'
    source.write_text(shim+firmware+main)
    qr_object=Path(root)/'qr.o'
    subprocess.run(['cc','-std=c11','-I'+str(qr_source.resolve()),'-c',str(qr_source/'lgfx_qrcode.c'),'-o',str(qr_object)],check=True)
    subprocess.run(['c++','-std=c++11','-I'+str(Path('firmware/family_dial').resolve()),str(source),str(qr_object),'-o',str(binary)],check=True)
    subprocess.run([str(binary),str(args.output.resolve())],check=True)
for ppm in args.output.glob('*.ppm'):
    data=ppm.read_bytes().split(b'\n',3)[3]
    def chunk(kind,payload):
        return struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload))
    raw=b''.join(b'\0'+data[y*720:(y+1)*720] for y in range(240))
    ppm.with_suffix('.png').write_bytes(b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',240,240,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b''))
manifest={'cases':[{'name':'join protected setup Wi-Fi','image':'wifi-join-qr.png','payload':'WIFI:T:WPA;S:Family-Radio-DEMO;P:synthetic-only-24-passkey;;'},{'name':'open portal page','image':'wifi-portal-qr.png','payload':'http://192.168.4.1/'},{'name':'retry failed join','image':'wifi-retry-qr.png','payload':'WIFI:T:WPA;S:Family-Radio-DEMO;P:synthetic-only-24-passkey;;'}]}
(args.output/'wifi-qr.json').write_text(json.dumps(manifest,indent=2))
layouts = len(list(args.output.glob('*.png')))
print(f'Actual firmware USB recovery, controls, window validation, title motion and {layouts} layouts passed:', args.output)
