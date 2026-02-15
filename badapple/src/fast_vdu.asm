; Fast VDP write — fills 16-byte UART FIFO in batches
; Ported from AgonSTNICCC (Optimus6128) to GNU as syntax
; Original: https://github.com/Optimus6128/AgonSTNICCC/blob/main/src/fast_vdu.asm

	.section .text,"ax",@progbits
	.assume adl = 1
	.global _fast_vdu

_fast_vdu:
	push	ix
	ld	ix, 0
	add	ix, sp

	push	hl
	push	de
	push	bc

	ld	hl, (ix + 6)		; data pointer
	ld	bc, (ix + 9)		; length

	call	uart0_fast_write

	pop	bc
	pop	de
	pop	hl
	pop	ix
	ret

uart0_fast_write:			; hl=data, bc=len
	push	hl

	ld	hl, -16
	or	a			; clear carry
	adc	hl, bc
	jp	nc, .Lwrite_lt_16	; < 16 bytes remaining

	; write 16-byte batch
	push	hl
	pop	bc			; bc -= 16
	pop	hl			; restore data ptr

	call	.Lwaitcts
.Lnot_ready:
	in0	a, (0xC5)		; UART0_LSR
	and	0x60			; TEMT | THRE (FIFO empty)
	jr	z, .Lnot_ready

	push	bc
	ld	b, 16
.Lfifo_fill:
	ld	a, (hl)
	inc	hl
	out0	(0xC0), a		; UART0_THR
	djnz	.Lfifo_fill
	pop	bc
	jp	uart0_fast_write

	; write final <16 bytes
.Lwrite_lt_16:
	pop	hl
	call	.Lwaitcts

.Lnot_ready2:
	in0	a, (0xC5)		; UART0_LSR
	and	0x60
	jr	z, .Lnot_ready2

	ld	b, c			; len < 16, fits in 8 bits
	ld	a, b
	or	a
	ret	z			; nothing left

.Lloop_lt_16:
	ld	a, (hl)
	inc	hl
	out0	(0xC0), a		; UART0_THR
	djnz	.Lloop_lt_16

.Lwaitcts:
	in0	a, (0xA2)		; Modem Status Register
	tst	a, 8			; CTS bit
	jr	nz, .Lwaitcts
	ret
