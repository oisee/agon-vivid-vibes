   10 REM Plasma Effect for Agon Light
   20 REM Inspired by Vivid Vibes demo
   30 REM Text mode plasma using 64 colors
   40 :
   50 MODE 8: REM 320x240, 64 colors, 40x30 text
   60 VDU 23,1,0: REM Hide cursor
   70 :
   80 REM Screen dimensions in characters
   90 W%=40: H%=30
  100 :
  110 REM Precompute sine table (256 entries, 0-255 range)
  120 DIM S%(255)
  130 FOR I%=0 TO 255
  140   S%(I%)=INT(127.5+127.5*SIN(I%*PI*2/256))
  150 NEXT
  160 :
  170 REM Precompute color lookup (64 colors)
  180 REM Map sine sum (0-1020) to color (0-63)
  190 DIM C%(255)
  200 FOR I%=0 TO 255
  210   C%(I%)=I% DIV 4
  220 NEXT
  230 :
  240 REM Animation loop
  250 T%=0
  260 REPEAT
  270   REM Draw plasma frame
  280   FOR Y%=0 TO H%-1
  290     FOR X%=0 TO W%-1
  300       REM Calculate plasma value from 4 waves
  310       V1%=S%((X%*8+T%) AND 255)
  320       V2%=S%((Y%*6+T%*2) AND 255)
  330       V3%=S%(((X%+Y%)*4+T%) AND 255)
  340       D%=SQR(((X%-20)*(X%-20)+(Y%-15)*(Y%-15))*4)
  350       V4%=S%((D%+T%) AND 255)
  360       REM Average of waves -> color
  370       V%=(V1%+V2%+V3%+V4%) DIV 16
  380       COL%=C%(V%)
  390       REM Set background color and print space
  400       VDU 31,X%,Y%
  410       VDU 17,128+COL%
  420       PRINT " ";
  430     NEXT
  440   NEXT
  450   T%=T%+1
  460 UNTIL INKEY(0)<>-1
  470 :
  480 REM Restore
  490 VDU 23,1,1: REM Show cursor
  500 MODE 0
  510 END
