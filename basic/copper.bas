   10 REM Copper Bars Effect for Agon Light
   20 REM Classic Amiga-style oscillating color bars
   30 REM Vivid Vibes style
   40 :
   50 MODE 3: REM 640x240, 64 colors
   60 VDU 23,1,0: REM Hide cursor
   70 :
   80 H%=30: REM Screen height in characters
   90 NB%=6: REM Number of bars
  100 BH%=5: REM Bar height in rows
  110 :
  120 REM Precompute sine table (256 entries)
  130 DIM S%(255)
  140 FOR I%=0 TO 255
  150   S%(I%)=INT(127.5+127.5*SIN(I%*PI*2/256))
  160 NEXT
  170 :
  180 REM Color gradient for each bar (warm copper tones)
  190 REM 64 color = RRGGBB (2 bits each)
  200 REM Create gradient from dark to bright to dark
  210 DIM G%(BH%-1)
  220 FOR I%=0 TO BH%-1
  230   REM Brightness peaks in middle
  240   B%=ABS(I%-(BH% DIV 2))
  250   B%=(BH% DIV 2)-B%
  260   G%(I%)=B%*2
  270 NEXT
  280 :
  290 CLS
  300 :
  310 REM Animation loop
  320 T%=0
  330 REPEAT
  340   REM Clear screen efficiently
  350   VDU 17,128: REM Black background
  360   CLS
  370   :
  380   REM Draw each bar
  390   FOR B%=0 TO NB%-1
  400     REM Calculate bar Y position (oscillating)
  410     PHASE%=(B%*256 DIV NB%+T%*4) AND 255
  420     YBASE%=(H% DIV 2)+(S%(PHASE%)-128)*(H%-BH%) DIV 256
  430     :
  440     REM Bar hue based on bar number + time
  450     HUE%=(B%*10+T% DIV 2) AND 63
  460     :
  470     REM Draw bar rows
  480     FOR R%=0 TO BH%-1
  490       Y%=YBASE%+R%
  500       IF Y%>=0 AND Y%<H% THEN
  510         REM Calculate row color with gradient
  520         BRIGHT%=G%(R%)
  530         REM Convert hue to RRGGBB (simple HSV approx)
  540         REM Hue 0-63: cycle through colors
  550         SEG%=HUE% DIV 11: REM 6 segments
  560         IF SEG%>5 THEN SEG%=5
  570         FRAC%=(HUE% MOD 11)*3 DIV 11
  580         IF FRAC%>3 THEN FRAC%=3
  590         IF SEG%=0 THEN R2%=3: G2%=FRAC%: B2%=0
  600         IF SEG%=1 THEN R2%=3-FRAC%: G2%=3: B2%=0
  610         IF SEG%=2 THEN R2%=0: G2%=3: B2%=FRAC%
  620         IF SEG%=3 THEN R2%=0: G2%=3-FRAC%: B2%=3
  630         IF SEG%=4 THEN R2%=FRAC%: G2%=0: B2%=3
  640         IF SEG%=5 THEN R2%=3: G2%=0: B2%=3-FRAC%
  650         REM Apply brightness
  660         R2%=(R2%*BRIGHT%) DIV 4
  670         G2%=(G2%*BRIGHT%) DIV 4
  680         B2%=(B2%*BRIGHT%) DIV 4
  690         IF R2%>3 THEN R2%=3
  700         IF G2%>3 THEN G2%=3
  710         IF B2%>3 THEN B2%=3
  720         COL%=(R2%*16)+(G2%*4)+B2%
  730         REM Draw full row
  740         VDU 31,0,Y%
  750         VDU 17,128+COL%
  760         PRINT STRING$(80," ");
  770       ENDIF
  780     NEXT R%
  790   NEXT B%
  800   :
  810   T%=T%+1
  820 UNTIL INKEY(0)<>-1
  830 :
  840 VDU 23,1,1
  850 MODE 0
  860 END
