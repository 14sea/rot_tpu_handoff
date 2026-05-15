# rot_tpu_handoff — cross-repo orchestrator for ROT → SD-staged TPU
# bitstream → ALTREMOTE_UPDATE self-reconfig flow on AX301.
#
# Plan reference: docs/plan.md (was ~/.claude/plans/temporal-booping-raven.md)
#
# This repo is purely orchestration + patches.  Three sibling repos
# (neorv32_rot, neorv32_tpu, EP4CE6) stay pristine; the firmware/host
# extensions live as git-am-style patches under patches/neorv32_rot/.

REPO         := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
PARENT       := $(abspath $(REPO)/..)

ROT_REPO    ?= $(PARENT)/neorv32_rot
TPU_REPO    ?= $(PARENT)/neorv32_tpu
EP4CE6_REPO ?= $(PARENT)/EP4CE6

QUARTUS_BIN ?= $(HOME)/intelFPGA_lite/21.1/quartus/bin
XPACK_BIN   ?= $(HOME)/xpack-riscv-none-elf-gcc-14.2.0-3/bin
OFL_LOADER  ?= $(HOME)/see_neorv32_run_linux/tools/openFPGALoader/build/openFPGALoader
ZETA_PIPELINE ?= $(EP4CE6_REPO)/scripts/bit_workaround/zeta_pipeline.py
ZETA_VERIFY ?= 1

QSH         := $(QUARTUS_BIN)/quartus_sh
QCPF        := $(QUARTUS_BIN)/quartus_cpf
PATH_FULL   := $(XPACK_BIN):$(QUARTUS_BIN):$(PATH)

PATCH_DIR    := $(REPO)/patches/neorv32_rot
PATCHES      := $(sort $(wildcard $(PATCH_DIR)/*.patch))
APPLIED_TAG  := $(ROT_REPO)/.rot_tpu_handoff_applied

TPU_QUARTUS  := $(TPU_REPO)/quartus
TPU_PROJECT  := neorv32_tpu
TPU_RBF_SRC  := $(TPU_REPO)/$(TPU_PROJECT).rbf
TPU_RBF_DST  := $(ROT_REPO)/output/tpu.rbf

ROT_QUARTUS  := $(ROT_REPO)/quartus
ROT_PROJECT  := neorv32_demo
ROT_RBF      := $(ROT_REPO)/output/$(ROT_PROJECT).rbf
ROT_FW_DIR   := $(ROT_REPO)/sw/stage2_loader

.PHONY: help apply-patches unapply-patches verify-pristine \
        tpu-then-rot tpu-bitstream tpu-zeta-verify tpu-copy \
        rot-firmware rot-bitstream rot-zeta-verify \
        sd-pack flash-rot clean-output

help:
	@echo "rot_tpu_handoff — cross-repo orchestrator"
	@echo
	@echo "Patch management:"
	@echo "  apply-patches      git am the patches into \$$ROT_REPO ($(ROT_REPO))"
	@echo "  unapply-patches    git reset \$$ROT_REPO back to pre-apply state"
	@echo "  verify-pristine    abort if \$$ROT_REPO has uncommitted changes"
	@echo
	@echo "Build chain (requires apply-patches first):"
	@echo "  tpu-then-rot       full chain: TPU bitstream → copy → ROT firmware → ROT bitstream"
	@echo "  tpu-bitstream      Quartus + cpf in \$$TPU_REPO/quartus"
	@echo "  tpu-zeta-verify    ζ byte-identity gate on TPU.rbf"
	@echo "  tpu-copy           tpu.rbf → \$$ROT_REPO/output/tpu.rbf"
	@echo "  rot-firmware       rebuild stage2 (gen_golden.py bakes TPU hash)"
	@echo "  rot-bitstream      Quartus + cpf in \$$ROT_REPO/quartus"
	@echo "  rot-zeta-verify    ζ byte-identity gate on ROT.rbf"
	@echo
	@echo "Hardware:"
	@echo "  sd-pack DEVICE=/dev/sdX   pack 4-payload SD blob"
	@echo "  flash-rot                  openFPGALoader -c usb-blaster"
	@echo
	@echo "  clean-output       wipe output/ artifacts (Quartus db/ kept)"
	@echo
	@echo "Configuration:"
	@echo "  ROT_REPO=$(ROT_REPO)"
	@echo "  TPU_REPO=$(TPU_REPO)"
	@echo "  EP4CE6_REPO=$(EP4CE6_REPO)"
	@echo "  QUARTUS_BIN=$(QUARTUS_BIN)"
	@echo "  XPACK_BIN=$(XPACK_BIN)"
	@echo "  OFL_LOADER=$(OFL_LOADER)"
	@echo "  ZETA_VERIFY=$(ZETA_VERIFY) (set 0 to skip)"

# ────────────────────────────────────────────────────────────────────
# Patch management — apply our extensions to a pristine ROT clone
# ────────────────────────────────────────────────────────────────────
verify-pristine:
	@cd $(ROT_REPO) && \
	  if ! git diff --quiet HEAD -- ':!neorv32' 2>/dev/null; then \
	      echo "[!] $(ROT_REPO) has uncommitted changes — refusing to apply patches"; \
	      git status --short | head; \
	      exit 1; \
	  fi
	@echo "[ok] $(ROT_REPO) is pristine (submodule + untracked artifacts ignored)"

apply-patches: verify-pristine
	@if [ -f $(APPLIED_TAG) ]; then \
	    echo "[!] patches already applied (marker $(APPLIED_TAG) exists)"; \
	    echo "    run 'make unapply-patches' first to revert"; \
	    exit 1; \
	fi
	@echo "[apply] git am $(words $(PATCHES)) patches into $(ROT_REPO)"
	@cd $(ROT_REPO) && git am --keep-cr $(PATCHES)
	@cd $(ROT_REPO) && git rev-parse HEAD > $(APPLIED_TAG)
	@echo "[apply] DONE — head now $$(cd $(ROT_REPO) && git rev-parse --short HEAD)"
	@echo "       saved tip to $(APPLIED_TAG) for unapply"

unapply-patches:
	@if [ ! -f $(APPLIED_TAG) ]; then \
	    echo "[!] no apply marker $(APPLIED_TAG) — nothing to unapply"; \
	    exit 1; \
	fi
	@target=$$(cd $(ROT_REPO) && git rev-parse "HEAD~$(words $(PATCHES))"); \
	  echo "[unapply] git reset --hard $$target in $(ROT_REPO)"; \
	  cd $(ROT_REPO) && git reset --hard $$target
	@rm -f $(APPLIED_TAG)
	@# auto-generated firmware artifacts (gitignored) may carry stale TPU
	@# hash from a prior gen_golden.py run; remove so next build is clean
	@rm -fv $(ROT_FW_DIR)/rot_golden.h $(TPU_RBF_DST) 2>/dev/null || true
	@echo "[unapply] DONE — patches removed from $(ROT_REPO)"

# ────────────────────────────────────────────────────────────────────
# Build chain — delegates to the (now-applied) ROT-side Makefile target
# ────────────────────────────────────────────────────────────────────
tpu-bitstream: $(TPU_RBF_SRC)

$(TPU_RBF_SRC): $(TPU_QUARTUS)/$(TPU_PROJECT).qsf
	@echo "[tpu] quartus_sh --flow compile in $(TPU_QUARTUS)"
	@cd $(TPU_QUARTUS) && PATH="$(PATH_FULL)" $(QSH) --flow compile $(TPU_PROJECT)
	@cd $(TPU_QUARTUS) && PATH="$(PATH_FULL)" $(QCPF) -c -o bitstream_compression=off \
	    output_files/$(TPU_PROJECT).sof ../$(TPU_PROJECT).rbf
	@ls -la $(TPU_RBF_SRC)

tpu-zeta-verify: $(TPU_RBF_SRC)
	@if [ "$(ZETA_VERIFY)" = "1" ]; then \
	    echo "[tpu] ζ byte-identity gate on $(TPU_RBF_SRC)"; \
	    PATH="$(PATH_FULL)" python3 $(ZETA_PIPELINE) $(TPU_RBF_SRC); \
	else echo "[tpu] ZETA_VERIFY=0 — skipping"; fi

tpu-copy: $(TPU_RBF_DST)
$(TPU_RBF_DST): $(TPU_RBF_SRC)
	@mkdir -p $(ROT_REPO)/output
	@cp -v $(TPU_RBF_SRC) $(TPU_RBF_DST)

rot-firmware: $(TPU_RBF_DST)
	@cd $(ROT_FW_DIR) && PATH="$(PATH_FULL)" $(MAKE) all
	@grep -E "TPU:|rot_golden_tpu_bitstream" $(ROT_FW_DIR)/rot_golden.h | head -3 || true

rot-bitstream: rot-firmware
	@cd $(ROT_QUARTUS) && PATH="$(PATH_FULL)" $(QSH) --flow compile $(ROT_PROJECT)
	@cd $(ROT_QUARTUS) && PATH="$(PATH_FULL)" $(QCPF) -c -o bitstream_compression=off \
	    output_files/$(ROT_PROJECT).sof ../output/$(ROT_PROJECT).rbf
	@ls -la $(ROT_RBF)

rot-zeta-verify: $(ROT_RBF)
	@if [ "$(ZETA_VERIFY)" = "1" ]; then \
	    echo "[rot] ζ byte-identity gate on $(ROT_RBF)"; \
	    PATH="$(PATH_FULL)" python3 $(ZETA_PIPELINE) $(ROT_RBF); \
	else echo "[rot] ZETA_VERIFY=0 — skipping"; fi

tpu-then-rot: tpu-bitstream tpu-zeta-verify tpu-copy rot-firmware rot-bitstream rot-zeta-verify
	@echo
	@echo "═══════════════════════════════════════════════════════════════"
	@echo " tpu-then-rot DONE"
	@echo "   TPU.rbf:  $(TPU_RBF_SRC)"
	@echo "   ROT.rbf:  $(ROT_RBF)  (with TPU hash baked into IMEM)"
	@echo "═══════════════════════════════════════════════════════════════"
	@echo "Next: make sd-pack DEVICE=/dev/sdX && make flash-rot"

# ────────────────────────────────────────────────────────────────────
# Hardware-side targets
# ────────────────────────────────────────────────────────────────────
sd-pack:
	@if [ -z "$(DEVICE)" ]; then \
	    echo "[!] usage: make sd-pack DEVICE=/dev/sdX"; exit 1; \
	fi
	@echo "[sd] packing 4-payload blob → $(DEVICE)"
	@cd $(ROT_REPO) && PATH="$(PATH_FULL)" python3 host/sd_pack.py \
	    --port /dev/ttyUSB0 \
	    --tpu-rbf $(TPU_RBF_DST) \
	    --skip-program

flash-rot: $(ROT_RBF)
	@echo "[flash] $(OFL_LOADER) -c usb-blaster -f $(ROT_RBF)"
	@$(OFL_LOADER) -c usb-blaster -f $(ROT_RBF)

clean-output:
	@rm -fv $(TPU_RBF_DST) $(ROT_RBF)
	@rm -rf $(TPU_QUARTUS)/output_files $(ROT_QUARTUS)/output_files
