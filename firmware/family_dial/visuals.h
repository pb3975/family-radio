#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

// One 112px RGB565 image. A complete CRC-checked transfer alone becomes visible.
struct Artwork {
  static const size_t SIZE = 112, BYTES = SIZE * SIZE * 2;
  uint16_t pixels[SIZE * SIZE] = {};
  char session[17] = "", key[9] = "-";
  int index = -1;
  size_t used = 0;
  bool ready = false;
  static int hex(char c) {
    return c >= '0' && c <= '9' ? c-'0' : c >= 'a' && c <= 'f' ? c-'a'+10 : -1;
  }
  static bool hexString(const char* s, size_t size) {
    if (strlen(s) != size) return false;
    for (size_t i=0;i<size;++i) if (hex(s[i]) < 0) return false;
    return true;
  }
  void bind(const char* nextSession, int nextIndex, const char* nextKey) {
    if (strcmp(session,nextSession) || index != nextIndex || strcmp(key,nextKey)) {
      strcpy(session,nextSession); strcpy(key,nextKey); index=nextIndex;
      used=0; ready=false;
    }
  }
  void clear() { session[0]=0; key[0]='-'; key[1]=0; index=-1; used=0; ready=false; }
  bool chunk(const char* incomingSession, int incomingIndex, const char* incomingKey, size_t offset, const char* data) {
    if (ready || strcmp(session,incomingSession) || index != incomingIndex || strcmp(key,incomingKey) || !strcmp(key,"-")) return false;
    size_t length=strlen(data);
    if (!length || length > 1024 || length%2 || offset+length/2 > BYTES || !hexString(data,length)) return false;
    // Replayed last chunks are idempotent when their progress ACK was lost.
    if (offset < used && offset+length/2 == used) return false;
    if (!offset) used=0;
    if (offset != used) return false;
    auto bytes=reinterpret_cast<uint8_t*>(pixels);
    for (size_t i=0;i<length;i+=2) bytes[used++]=(hex(data[i])<<4)|hex(data[i+1]);
    if (used == BYTES) {
      uint32_t crc=0xffffffff, expected=0;
      for (size_t i=0;i<BYTES;++i) {
        crc^=bytes[i];
        for (int bit=0;bit<8;++bit) crc=(crc>>1) ^ ((crc&1)?0xedb88320:0);
      }
      for (int i=0;i<8;++i) expected=(expected<<4)|hex(key[i]);
      ready=(~crc)==expected;
      if (!ready) used=0;
    }
    return ready;
  }
};
