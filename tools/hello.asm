;
; hello.asm — Minimal MOS "Hello World" to validate toolchain
;
; Assembled with: ez80asm hello.asm hello.bin
;

			.ASSUME	ADL = 1

			JP	_start			; Jump over header

_exec_name:		DB	"hello.bin",0		; Executable name

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

			; Print "Hello from eZ80!" one char at a time via RST $10
			LD	HL, msg
_loop:
			LD	A, (HL)
			OR	A			; end of string?
			JR	Z, _done
			RST.LIL	10h		; Send char to VDP
			INC	HL
			JR	_loop

_done:
			POP	AF
			LD	MB, A			; Restore MB
			POP	IY
			POP	IX
			POP	DE
			POP	BC
			POP	AF
			RET

msg:			DB	"Hello from eZ80 ADL mode!", 13, 10, 0
