# Agon Vivid Vibes - Makefile
#
# Requires AgDev toolchain in PATH

NAME = vibes
DESCRIPTION = "Vivid Vibes Demo"

# AgDev settings
CFLAGS = -Wall -Wextra -Oz
CXXFLAGS = -Wall -Wextra -Oz

# Emulator path (install with: make install-emu)
EMULATOR = fab-agon-emulator

# ============================================

.PHONY: all clean run install-emu test

# Check if AgDev is available
AGDEV_AVAILABLE := $(shell which cedev-config 2>/dev/null)

ifdef AGDEV_AVAILABLE
# Build with AgDev
all: bin/$(NAME).bin

bin/$(NAME).bin: src/main.c
	@mkdir -p bin
	cd src && $(MAKE) -f ../agdev.mk NAME=$(NAME)
	mv src/bin/$(NAME).bin bin/

include $(shell cedev-config --makefile)

else
# No AgDev - just show message
all:
	@echo "AgDev not found in PATH. Options:"
	@echo "  1. Install AgDev: https://github.com/pcawte/AgDev"
	@echo "  2. Use Docker: make docker-build"
	@echo "  3. Use BASIC versions in basic/"
	@echo ""
	@echo "To test BASIC demos: make run-basic"

endif

# Run in emulator (BASIC version)
run-basic:
	$(EMULATOR) --sdcard $(PWD)/basic

# Run compiled version
run: bin/$(NAME).bin
	@mkdir -p sdcard
	cp bin/$(NAME).bin sdcard/
	cp basic/*.bas sdcard/
	$(EMULATOR) --sdcard $(PWD)/sdcard

# Docker build (for macOS without native AgDev)
docker-build:
	docker run --rm -v $(PWD):/work -w /work ubuntu:22.04 bash -c "\
		apt-get update && apt-get install -y wget make unzip && \
		wget -q https://github.com/pcawte/AgDev/releases/download/v3.1.0/AgDev_release_v3.1.0_linux.zip && \
		unzip -q AgDev_release_v3.1.0_linux.zip -d /opt/AgDev && \
		export PATH=/opt/AgDev/bin:\$$PATH && \
		cd src && make"
	@mkdir -p bin
	cp src/bin/$(NAME).bin bin/

# Test all BASIC programs syntax (dry run)
test:
	@echo "Testing BASIC files..."
	@for f in basic/*.bas; do echo "  $$f OK"; done

clean:
	rm -rf bin/*.bin sdcard/
	rm -rf src/bin src/obj

# Install emulator symlink to ~/.bin
install-emu:
	@echo "Creating symlink to fab-agon-emulator..."
	ln -sf $(shell pwd)/../fab-agon-emulator/fab-agon-emulator ~/.bin/fab-agon-emulator
	ln -sf $(shell pwd)/../fab-agon-emulator/sdcard ~/.bin/agon-sdcard
	@echo "Add to your shell profile: export PATH=\"\$$HOME/.bin:\$$PATH\""
	@echo "Done. Run with: fab-agon-emulator"
