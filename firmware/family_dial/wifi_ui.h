#pragma once
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

struct WifiUI {
  char setupId[33]={}, state[20]="IDLE", error[16]="-";
  char ssid[33]={}, password[64]={}, payload[256]={};
  uint32_t requestId=0, revision=0, remaining=0, receivedAt=0, sentAt=0;
  char command=0;
  bool pending=false;

  static void erase(char* value,size_t size){volatile char* p=value;while(size--)*p++=0;}
  void clearAccess() {erase(ssid,sizeof(ssid));erase(password,sizeof(password));erase(payload,sizeof(payload));}
  void reset() {
    clearAccess();memset(setupId,0,sizeof(setupId));strcpy(state,"IDLE");strcpy(error,"-");
    requestId=revision=remaining=receivedAt=sentAt=0;command=0;pending=false;
  }
  void request(char op) {
    if(requestId>=2147483647) return;
    ++requestId;command=op;pending=true;sentAt=0;
    if(op!='Q')clearAccess();
    if(op=='S')strcpy(state,"STARTING");
    if(op=='T')strcpy(state,"STOPPING");
  }
  bool due(uint32_t now) const {return pending && (!sentAt || uint32_t(now-sentAt)>=1500);}
  bool terminal() const {return !strcmp(state,"CONNECTED")||!strcmp(state,"CANCELLED")||!strcmp(state,"EXPIRED")||!strcmp(state,"ERROR")||!strcmp(state,"RECOVERY_FAILED")||!strcmp(state,"PERSISTENCE_FAILED");}
  bool hasAccess() const {return *payload && (!strcmp(state,"READY") || !strcmp(state,"FAILED"));}
  void tick(uint32_t now) {
    if(!receivedAt)return;
    if(hasAccess() && uint32_t(now-receivedAt)>=remaining*1000u) {
      clearAccess();strcpy(state,"EXPIRED");remaining=0;
    } else if(hasAccess() && uint32_t(now-receivedAt)>8000) {
      clearAccess();strcpy(state,"ERROR");strcpy(error,"UNAVAILABLE");
    }
  }
  static bool identity(const char* value) {
    if(strlen(value)!=32)return false;
    for(const char* p=value;*p;++p)if(!((*p>='0'&&*p<='9')||(*p>='a'&&*p<='f')))return false;
    return true;
  }
  static int hex(char c) {return c>='0'&&c<='9'?c-'0':c>='a'&&c<='f'?c-'a'+10:-1;}
  static bool decode(const char* value,char* output,size_t capacity,bool ascii) {
    size_t n=strlen(value);
    if(!strcmp(value,"-")){output[0]=0;return true;}
    if(!n || n%2 || n/2>=capacity)return false;
    for(size_t i=0;i<n;i+=2) {
      int a=hex(value[i]),b=hex(value[i+1]);if(a<0||b<0)return false;
      unsigned char c=(a<<4)|b;
      if(c<32 || c==127 || (ascii&&c>126))return false;
      output[i/2]=c;
    }
    output[n/2]=0;return true;
  }
  static bool integer(const char* value,uint32_t limit,uint32_t& result) {
    if(!*value || strlen(value)>10)return false;
    uint64_t n=0;for(const char* p=value;*p;++p){if(*p<'0'||*p>'9')return false;n=n*10+*p-'0';}
    if(n>limit)return false;result=n;return true;
  }
  bool receive(char** f,uint32_t now) {
    uint32_t request, rev, seconds;
    if(!integer(f[3],2147483647,request)||request!=requestId||!request ||
       !integer(f[5],2147483647,rev)||!integer(f[7],600,seconds))return false;
    if(strcmp(f[4],"-")&&!identity(f[4]))return false;
    const char* states[]={"IDLE","STARTING","READY","CONNECTING","FAILED","CONNECTED","STOPPING","CANCELLED","EXPIRED","RECOVERY_FAILED","PERSISTENCE_FAILED","ERROR"};
    bool valid=false;for(const char* s:states)if(!strcmp(f[6],s))valid=true;if(!valid)return false;
    const char* errors[]={"-","NETWORK","RECOVERY","PERSISTENCE","CANCELLED","START","UNAVAILABLE"};
    valid=false;for(const char* s:errors)if(!strcmp(f[11],s))valid=true;if(!valid)return false;
    bool same=*setupId && !strcmp(setupId,f[4]);
    if(same && rev<revision)return false;
    if(*setupId && !same && command!='P' && command!='S')return false;
    if(!pending && rev==revision && !strcmp(f[4],*setupId?setupId:"-") && !strcmp(f[6],state))return false;
    char nextSsid[33]={}, nextPassword[64]={},nextPayload[256]={};
    struct Erase {char* p;size_t n;~Erase(){WifiUI::erase(p,n);}} secret1{nextPassword,sizeof(nextPassword)},secret2{nextPayload,sizeof(nextPayload)};
    if(!decode(f[8],nextSsid,sizeof(nextSsid),false)||!decode(f[9],nextPassword,sizeof(nextPassword),true)||
       !decode(f[10],nextPayload,sizeof(nextPayload),false))return false;
    bool access=*nextSsid||*nextPassword||*nextPayload;
    if(access && (!*nextSsid||strlen(nextPassword)<8||strncmp(nextPayload,"WIFI:",5)||!seconds||
        (!strcmp(f[4],"-")) || (strcmp(f[6],"READY")&&strcmp(f[6],"FAILED"))))return false;
    if((!strcmp(f[6],"READY")||!strcmp(f[6],"FAILED"))&&!access)return false;
    clearAccess();strcpy(ssid,nextSsid);strcpy(password,nextPassword);strcpy(payload,nextPayload);
    memset(nextPassword,0,sizeof(nextPassword));memset(nextPayload,0,sizeof(nextPayload));
    strcpy(setupId,!strcmp(f[4],"-")?"":f[4]);strcpy(state,f[6]);strcpy(error,f[11]);
    revision=rev;remaining=seconds;receivedAt=now;pending=false;return true;
  }
};
