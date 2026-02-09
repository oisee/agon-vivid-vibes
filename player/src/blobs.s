	.assume adl=1
	.section .rodata

	.global _setup_data
	.global _setup_data_end
	.global _cube_compressed
	.global _cube_compressed_end
	.global _torus_compressed
	.global _torus_compressed_end

_setup_data:
	.incbin "cube_setup.bin"
_setup_data_end:

_cube_compressed:
	.incbin "cube_compressed.bin"
_cube_compressed_end:

_torus_compressed:
	.incbin "torus_compressed.bin"
_torus_compressed_end:

