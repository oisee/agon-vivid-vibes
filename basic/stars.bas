   10 REM Starfield Effect for Agon Light
   20 REM Classic hyperspace warp effect
   30 REM Vivid Vibes style
   40 :
   50 MODE 8: REM 320x240, 64 colors
   60 VDU 23,1,0: REM Hide cursor
   70 :
   80 W%=320: H%=240
   90 CX%=W% DIV 2: CY%=H% DIV 2
  100 NS%=100: REM Number of stars
  110 :
  120 REM Star arrays: X, Y, Z (perspective depth)
  130 DIM SX(NS%), SY(NS%), SZ(NS%)
  140 :
  150 REM Initialize stars at random positions
  160 FOR I%=1 TO NS%
  170   SX(I%)=RND(W%)-CX%
  180   SY(I%)=RND(H%)-CY%
  190   SZ(I%)=RND(256)
  200 NEXT
  210 :
  220 CLS
  230 :
  240 REM Animation loop
  250 REPEAT
  260   REM Update and draw each star
  270   FOR I%=1 TO NS%
  280     REM Erase old position
  290     IF SZ(I%)<256 THEN
  300       SCALE%=256 DIV (SZ(I%)+1)
  310       OX%=CX%+SX(I%)*SCALE% DIV 32
  320       OY%=CY%+SY(I%)*SCALE% DIV 32
  330       IF OX%>=0 AND OX%<W% AND OY%>=0 AND OY%<H% THEN
  340         GCOL 0,0: REM Black
  350         PLOT 69,OX%,OY%: REM Plot point
  360       ENDIF
  370     ENDIF
  380     :
  390     REM Move star closer (decrease Z)
  400     SZ(I%)=SZ(I%)-4
  410     :
  420     REM Reset star if it passed camera
  430     IF SZ(I%)<1 THEN
  440       SX(I%)=RND(W%)-CX%
  450       SY(I%)=RND(H%)-CY%
  460       SZ(I%)=256
  470     ENDIF
  480     :
  490     REM Calculate new screen position
  500     SCALE%=256 DIV (SZ(I%)+1)
  510     NX%=CX%+SX(I%)*SCALE% DIV 32
  520     NY%=CY%+SY(I%)*SCALE% DIV 32
  530     :
  540     REM Draw if on screen
  550     IF NX%>=0 AND NX%<W% AND NY%>=0 AND NY%<H% THEN
  560       REM Brightness based on depth (closer = brighter)
  570       BR%=63-SZ(I%) DIV 4
  580       IF BR%<0 THEN BR%=0
  590       IF BR%>63 THEN BR%=63
  600       GCOL 0,BR%
  610       PLOT 69,NX%,NY%
  620     ENDIF
  630   NEXT
  640 UNTIL INKEY(0)<>-1
  650 :
  660 VDU 23,1,1
  670 MODE 0
  680 END
