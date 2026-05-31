// wb_tpu_accel.v — Wishbone (NEORV32 XBUS) to tpu_accel bridge
//
// v2 (2026-05-20 Plan C-1 dynamic-load demo): widens forwarded address from
// [5:0] to [11:0] so tpu_accel can host an additional LUT memory window.
//
// Maps NEORV32 XBUS Wishbone cycles to the tpu_accel.v interface.
// Top-level decode passes anything in 0xF0000000–0xF00FFFFF here; tpu_accel
// internally selects between systolic regs (0x000-0x03F) and LUT memory
// (0x040-0x43F).  Above 0x440 is reserved / reads zero.

module wb_tpu_accel (
    input         clk,
    input         rst_n,

    // NEORV32 XBUS (Wishbone-compatible) — directly from top-level decode
    input  [31:0] xbus_adr,
    input  [31:0] xbus_dat_w,
    input  [3:0]  xbus_sel,
    input         xbus_we,
    input         xbus_stb,
    input         xbus_cyc,
    output reg [31:0] xbus_dat_r,
    output reg    xbus_ack,
    output        xbus_err,

    // Debug outputs
    output [3:0]  dbg_leds
);

    // ── tpu_accel internal wires ────────────────────────────────────────────
    wire [31:0] tpu_rdata;
    wire        tpu_ready;
    wire [3:0]  tpu_debug;

    // ── Pending transaction tracker ──────────────────────────────────────────
    // Same pattern as wb_sdram_ctrl: assert tpu sel for one cycle,
    // hold pending HIGH until tpu_accel pulses ready.
    reg pending;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            pending    <= 1'b0;
            xbus_ack   <= 1'b0;
            xbus_dat_r <= 32'd0;
        end else begin
            xbus_ack <= 1'b0;  // default: deasserted

            if (xbus_cyc && xbus_stb && !pending) begin
                pending <= 1'b1;           // start TPU transaction
            end

            if (pending && tpu_ready) begin
                xbus_dat_r <= tpu_rdata;
                xbus_ack   <= 1'b1;
                pending    <= 1'b0;
            end
        end
    end

    assign xbus_err = 1'b0;

    // ── Debug: directly forward tpu_accel debug LEDs ─────────────────────
    assign dbg_leds = tpu_debug;

    // ── tpu_accel instantiation ──────────────────────────────────────────────
    tpu_accel u_tpu (
        .clk       (clk),
        .rst_n     (rst_n),
        .sel       (pending),
        .addr      (xbus_adr[11:0]),
        .wdata     (xbus_dat_w),
        .wstrb     (xbus_we ? xbus_sel : 4'b0000),
        .rdata     (tpu_rdata),
        .ready     (tpu_ready),
        .debug_led (tpu_debug)
    );

endmodule
