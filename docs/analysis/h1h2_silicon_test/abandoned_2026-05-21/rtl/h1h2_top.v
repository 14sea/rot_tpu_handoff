// h1h2_top.v --- H1/H2 silicon discriminator
//
// One sentinel cycloneive_lcell_comb at LCCOMB_X10_Y2_N0 plus 15 pass-through
// LUTs locked to N=2..30 in the same LAB column. The chain (jbscan-style)
// forces Quartus Lite to actually honor the per-LE location locks; a lone
// LE without surrounding chain-mates was silently placed elsewhere.
//
// u0 holds the test LUT (baseline mask=0xA5A5). u1..u15 are pass-through
// (mask=0xAAAA = output dataa). The chain output drives LED[0]; because
// pass-throughs are transparent, LED[0] == u0.combout for the current sweep.
//
// edit_rbf.py XORs (target_mask ^ 0xA5A5) onto cells at (X=10, Y=2, N=0)
// to realize any 16-bit target truth table on u0.
//
// Pin map (AX301, EP4CE10F17C8 -- see reference-ax301-board-quirks):
//   CLOCK PIN_E1   50 MHz oscillator
//   LED0  PIN_G15  combout of u0 (carried through pass-through chain)
//   LED1  PIN_F16  sweep[1]
//   LED2  PIN_F15  sweep[2]
//   LED3  PIN_D16  sweep[3]
// sweep[0] is inferred from LED[3:1] transitions (see capture template).
// KEY2 is NOT used (E16 lacks weak pull-up on EP4CE10).

module h1h2_top (
    input  wire       CLOCK,
    output wire [3:0] LED
);

    // Power-on reset.
    reg [3:0] por_cnt = 4'd0;
    always @(posedge CLOCK) begin
        if (por_cnt[3] == 1'b0) por_cnt <= por_cnt + 1'b1;
    end
    wire rstn = por_cnt[3];

    // ~0.75 Hz tick (50 MHz / 2^26).
    reg [25:0] divcnt;
    always @(posedge CLOCK or negedge rstn) begin
        if (!rstn) divcnt <= 26'd0;
        else       divcnt <= divcnt + 1'b1;
    end
    wire tick = (divcnt == 26'd0);

    // 4-bit sweep through all 16 minterms.
    reg [3:0] sweep;
    always @(posedge CLOCK or negedge rstn) begin
        if (!rstn)     sweep <= 4'd0;
        else if (tick) sweep <= sweep + 1'b1;
    end

    // u0 -- sentinel. lut_mask=0xA5A5 (= sweep[0] ^ sweep[1] ^ sweep[2]) so
    // the LE has non-trivial input dependence and Quartus can't constant-
    // fold it. Host editor XORs (target ^ 0xA5A5) at (10,2,0) to realize
    // any target TT.
    (* keep = 1, preserve = 1 *) wire w_sentinel;
    cycloneive_lcell_comb #(
        .lut_mask       (16'hC396),
        .sum_lutc_input ("datac")
    ) u0 (
        .dataa   (sweep[0]),
        .datab   (sweep[1]),
        .datac   (sweep[2]),
        .datad   (sweep[3]),
        .cin     (1'b0),
        .combout (w_sentinel),
        .cout    ()
    );

    // u1..u15 -- pass-through chain (lut_mask=0xAAAA = output dataa) to
    // force Quartus to occupy the rest of the LAB column at X=10, Y=2.
    // The provider's jbscan project proved that a long chain of locked
    // cycloneive_lcell_comb instances is what actually makes Lite Edition
    // honor LCCOMB_X*_Y*_N* assignments.
    wire [15:0] chain /* synthesis keep */;
    assign chain[0] = w_sentinel;

    genvar i;
    generate
        for (i = 1; i < 16; i = i + 1) begin: padlut
            (* keep = 1, preserve = 1 *) wire w;
            cycloneive_lcell_comb #(
                .lut_mask       (16'hAAAA),
                .sum_lutc_input ("datac")
            ) u (
                .dataa   (chain[i-1]),
                .datab   (1'b0),
                .datac   (1'b0),
                .datad   (1'b0),
                .cin     (1'b0),
                .combout (w),
                .cout    ()
            );
            assign chain[i] = w;
        end
    endgenerate

    // chain[15] = pass-through of pass-through of ... of w_sentinel.
    assign LED = {sweep[3], sweep[2], sweep[1], chain[15]};

endmodule
