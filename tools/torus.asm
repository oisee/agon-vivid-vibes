;
; torus.asm — Spinning torus MOS executable
;
; Assembled with: ez80asm torus.asm torus.bin
;

			.ASSUME	ADL = 1

			JP	_start			; Jump over header

_exec_name:		DB	"torus.bin",0		; Executable name

			ALIGN	64

			DB	"MOS"			; MOS magic
			DB	00h			; Header version 0
			DB	01h			; ADL mode

_start:
			PUSH	AF
			PUSH	BC
			PUSH	DE
			PUSH	IX
			PUSH	IY

			LD	A, MB			; Save MB
			PUSH	AF
			XOR	A
			LD	MB, A			; Clear MB for 24-bit MOS API

			; Get system variables pointer
			LD	A, 08h			; mos_sysvars
			RST.LIL	08h			; IX = sysvars

			; Send VDU setup (mode 8, pixel coords, cursor off, CLS, CLG)
			LD	HL, setup_data
			LD	BC, 11
			RST.LIL	18h			; Send BC bytes from HL to VDP

			; Wait for mode switch to settle
			LD	B, 15
			CALL	wait_ticks

; ---- Main animation loop ----
main_loop:
			LD	HL, frame_data + 2	; Skip num_frames u16 header
			LD	DE, 90

next_frame:
			; Read frame length: 16-bit LE at (HL)
			LD	C, (HL)
			INC	HL
			LD	B, (HL)
			INC	HL			; HL = frame VDU data start

			PUSH	DE			; save frame counter
			PUSH	HL			; save data pointer
			PUSH	BC			; save frame length

			; Send frame to VDP
			RST.LIL	18h			; HL=addr, BC=len

			; Swap framebuffer (VDU 23, 0, 0xC3) — also waits for vsync
			LD	HL, swap_cmd
			LD	BC, 3
			RST.LIL	18h

			; Wait one tick for swap to complete
			LD	B, 1
			CALL	wait_ticks

			; Check for space key (sysvar_keyascii = IX+5)
			LD	A, (IX+5)
			CP	20h			; space?
			JR	Z, _exit

			POP	BC			; restore frame length
			POP	HL			; restore data pointer
			POP	DE			; restore frame counter

			; Advance HL past frame data
			ADD	HL, BC

			; Decrement frame counter, loop if not zero
			DEC	DE
			LD	A, D
			OR	E
			JR	NZ, next_frame

			; Loop forever
			JP	main_loop

_exit:
			POP	BC			; clean up stack
			POP	HL
			POP	DE

			; Restore mode 0 + cursor
			LD	HL, restore_data
			LD	BC, 5
			RST.LIL	18h

			POP	AF
			LD	MB, A			; Restore MB
			POP	IY
			POP	IX
			POP	DE
			POP	BC
			POP	AF
			RET

; ---- Subroutine: wait B ticks of sysvar_time ----
wait_ticks:
wt_outer:
			LD	A, (IX+0)		; current sysvar_time low byte
			LD	C, A			; snapshot
wt_inner:
			LD	A, (IX+0)		; re-read
			CP	C			; changed?
			JR	Z, wt_inner		; no — keep spinning
			DJNZ	wt_outer		; yes — one tick done
			RET

; ---- Data ----
setup_data:
			INCBIN	"torus_setup.bin"

swap_cmd:
			DB	23, 0, 0C3h		; VDU 23, 0, &C3 — swap buffers

restore_data:
			DB	22, 0			; VDU 22, 0 — mode 0
			DB	23, 1, 1		; cursor on

frame_data:
			INCBIN	"torus_frames.bin"
