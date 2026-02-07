   10 REM Optimized Plasma Effect for Agon Light
   20 REM Uses precomputed lookup tables for speed
   30 REM Vivid Vibes style
   40 :
   50 MODE 8: REM 320x240, 64 colors, 40x30 text
   60 VDU 23,1,0: REM Hide cursor
   70 :
   80 REM Screen dimensions
   90 W%=40: H%=30
  100 CX%=W% DIV 2: CY%=H% DIV 2
  110 :
  120 REM Precompute sine table (256 entries, 0-63 range)
  130 DIM S%(255)
  140 FOR I%=0 TO 255
  150   S%(I%)=INT(31.5+31.5*SIN(I%*PI*2/256))
  160 NEXT
  170 :
  180 REM Precompute distance table for radial wave
  190 DIM D%(W%*H%-1)
  200 FOR Y%=0 TO H%-1
  210   FOR X%=0 TO W%-1
  220     DX%=X%-CX%: DY%=Y%-CY%
  230     D%(Y%*W%+X%)=INT(SQR(DX%*DX%+DY%*DY%)*6) AND 255
  240   NEXT
  250 NEXT
  260 PRINT "Tables ready..."
  270 :
  280 REM Clear and start
  290 CLS
  300 :
  310 REM Animation loop
  320 T%=0
  330 REPEAT
  340   P%=0: REM Position in distance table
  350   FOR Y%=0 TO H%-1
  360     VDU 31,0,Y%: REM Move to start of row
  370     FOR X%=0 TO W%-1
  380       REM Wave 1: horizontal
  390       V1%=S%((X%*6+T%) AND 255)
  400       REM Wave 2: vertical + time
  410       V2%=S%((Y%*8+T%*2) AND 255)
  420       REM Wave 3: diagonal
  430       V3%=S%(((X%+Y%)*4+T%) AND 255)
  440       REM Wave 4: radial (precomputed distance)
  450       V4%=S%((D%(P%)+T%) AND 255)
  460       REM Sum waves and convert to color
  470       COL%=((V1%+V2%+V3%+V4%) DIV 4) AND 63
  480       VDU 17,128+COL%
  490       VDU 32: REM Print space
  500       P%=P%+1
  510     NEXT
  520   NEXT
  530   T%=T%+2
  540 UNTIL INKEY(0)<>-1
  550 :
  560 VDU 23,1,1
  570 MODE 0
  580 END
