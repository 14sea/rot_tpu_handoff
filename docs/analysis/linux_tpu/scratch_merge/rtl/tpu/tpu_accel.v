// TPU Accelerator — Memory-mapped wrapper for 4x4 systolic array
//                   + 256-entry LUT memory (v2, Plan C-1 dynamic-load target)
//                   + LUT->weight auto-load FSM (v3, Step 3 compute-path wire-in)
//
// Register map (active when sel=1):
//   --- Systolic-array region (0x000-0x03F) ---
//   0x000 CTRL    (W)  [0]=sa_en (start compute), [4]=clear (reset acc+done),
//                       [8]=load_from_lut (v3 one-shot: copy LUT[0..15][23:16]
//                            into PE weights — PE(row,col) gets
//                            LUT[row*4+col] bits [23:16].  Bits [23:16] match
//                            mask[7:0] in the firmware-side packing
//                            convention for edits.bin records, so this byte
//                            varies across consecutive records.)
//   0x004 STATUS  (R)  [0]=done, [1]=lut_load_busy (v3)
//   0x008 W_ADDR  (W)  [1:0]=col, [3:2]=row
//   0x00C W_DATA  (W)  [7:0]=weight byte (triggers 1-cycle load_weight pulse)
//   0x010 X_IN    (W)  [31:0]={x3,x2,x1,x0} packed signed int8
//   0x014 W_DATA4 (W)  [31:0]={w3,w2,w1,w0} packed int8
//                       Loads 4 weights into current row (col 0-3), auto-increments row.
//                       Bus stalls for 3 extra cycles during bulk load.
//   0x020 RES0    (R)  int32 row 0 accumulator
//   0x024 RES1    (R)  int32 row 1 accumulator
//   0x028 RES2    (R)  int32 row 2 accumulator
//   0x02C RES3    (R)  int32 row 3 accumulator
//
//   --- LUT memory region (0x040-0x43F) ---
//   0x040 + i*4 (R/W)  256 entries × 32 bits, M9K-backed.
//                       Suggested packing for mode_g records:
//                         [31:16] = mask, [15:11] = n, [10:5] = y, [4:0] = x
//                       But the BRAM stores 32 bits verbatim — encoding is a
//                       firmware-side convention only.  Used by mode_g
//                       (Plan C-1) to demonstrate SD→on-chip dynamic loading
//                       with trust-anchor verification, replacing the dead
//                       AS-REMOTE-driven Phase 8 path.
//
// Compute sequence (after weights loaded):
//   1. Write X_IN with packed inputs
//   2. Write CTRL[0]=1 to start
//   3. Hardware runs 10-cycle pipeline (skewed injection + drain)
//   4. STATUS.done goes high; results accumulated into RES0-3
//   5. Write CTRL[4]=1 to clear accumulators before next output group

module tpu_accel (
    input  wire        clk,
    input  wire        rst_n,
    // Bus interface
    input  wire        sel,
    input  wire [11:0] addr,       // byte address [11:0] (widened from [5:0] in v2)
    input  wire [31:0] wdata,
    input  wire [ 3:0] wstrb,
    output reg  [31:0] rdata,
    output reg         ready,
    // Systolic array result (directly from SA)
    // (SA is instantiated inside this module)
    output wire [3:0]  debug_led   // optional debug
);

    // ---- Address-window decode (v2) ----
    // word_addr is the dword index (byte addr >> 2).
    wire [9:0] word_addr = addr[11:2];
    wire       in_systolic = (word_addr[9:4] == 6'h00);                       // 0x000-0x03F
    wire       in_lut      = (word_addr >= 10'h010) && (word_addr <= 10'h10F); // 0x040-0x43F
    wire [9:0] lut_off10   = word_addr - 10'h010;
    wire [7:0] lut_idx     = lut_off10[7:0];

    // ---- Systolic array instance ----
    reg         sa_en;
    reg         sa_load_weight;
    reg  [1:0]  sa_w_row_sel;
    reg  [1:0]  sa_w_col_sel;
    reg  signed [7:0] sa_w_data;
    reg  [31:0] sa_x_in;
    wire [127:0] sa_result;

    systolic_array_4x4 sa_inst (
        .clk        (clk),
        .rst_n      (rst_n),
        .en         (sa_en),
        .load_weight(sa_load_weight),
        .w_row_sel  (sa_w_row_sel),
        .w_col_sel  (sa_w_col_sel),
        .w_data     (sa_w_data),
        .x_in       (sa_x_in),
        .result     (sa_result)
    );

    // Unpack SA results
    wire signed [31:0] sa_res [0:3];
    assign sa_res[0] = sa_result[ 31:  0];
    assign sa_res[1] = sa_result[ 63: 32];
    assign sa_res[2] = sa_result[ 95: 64];
    assign sa_res[3] = sa_result[127: 96];

    // ---- Registers ----
    reg [31:0] x_in_reg;         // stored X_IN
    reg [1:0]  w_addr_row;
    reg [1:0]  w_addr_col;

    // ---- Accumulators ----
    reg signed [31:0] acc [0:3];

    // ---- Bulk weight load (W_DATA4) ----
    reg        bulk_active;      // bulk load in progress
    reg [1:0]  bulk_phase;       // 0=col1, 1=col2, 2=col3 (col0 loaded on trigger)
    reg [31:0] bulk_data;        // latched packed weights
    reg [1:0]  bulk_row;         // row being loaded

    // ---- LUT->weight auto-load FSM (v3) ----
    // Triggered by CTRL[8]=1.  Cycles through LUT[0..15] in idx order,
    // takes each entry's low 8 bits as a signed weight byte, drives the
    // existing load_weight infrastructure to place it at PE(idx/4, idx%4).
    // 2 cycles per entry (phase 0 = address present, phase 1 = data
    // captured + load_weight pulse), so 32 cycles total + 2 finish cycles.
    reg        lut_load_active;
    reg [4:0]  lut_load_idx;     // 0..15 valid; 16 = done sentinel
    reg        lut_load_phase;   // 0 = present addr, 1 = drive load_weight

    // ---- Compute FSM ----
    reg [3:0] compute_cnt;       // 0 = idle, 1-10 = computing
    reg       done;

    wire computing = (compute_cnt != 4'd0);

    // Skewed input injection
    always @(*) begin
        case (compute_cnt)
            4'd1: sa_x_in = {24'd0, x_in_reg[7:0]};               // x0 only
            4'd2: sa_x_in = {16'd0, x_in_reg[15:8], 8'd0};        // x1 only
            4'd3: sa_x_in = {8'd0, x_in_reg[23:16], 16'd0};       // x2 only
            4'd4: sa_x_in = {x_in_reg[31:24], 24'd0};             // x3 only
            default: sa_x_in = 32'd0;
        endcase
    end

    // SA enable during compute
    always @(*) begin
        sa_en = computing;
    end

    // Accumulate results at cnt = 5,6,7,8
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            compute_cnt <= 4'd0;
            done <= 1'b0;
            acc[0] <= 32'sd0;
            acc[1] <= 32'sd0;
            acc[2] <= 32'sd0;
            acc[3] <= 32'sd0;
            bulk_active <= 1'b0;
            bulk_phase <= 2'd0;
            bulk_data <= 32'd0;
            bulk_row <= 2'd0;
            sa_load_weight <= 1'b0;
            w_addr_row <= 2'd0;
            w_addr_col <= 2'd0;
            lut_load_active <= 1'b0;
            lut_load_idx <= 5'd0;
            lut_load_phase <= 1'b0;
        end else begin
            // Weight load pulse — auto-clear after 1 cycle (only when neither
            // bulk nor LUT-load FSM is actively driving it)
            if (sa_load_weight && !bulk_active && !lut_load_active)
                sa_load_weight <= 1'b0;

            // Compute counter
            if (computing) begin
                if (compute_cnt == 4'd9) begin
                    compute_cnt <= 4'd0;
                    done <= 1'b1;
                end else begin
                    compute_cnt <= compute_cnt + 4'd1;
                end

                // Accumulate at the right cycles
                if (compute_cnt == 4'd5) acc[0] <= acc[0] + sa_res[0];
                if (compute_cnt == 4'd6) acc[1] <= acc[1] + sa_res[1];
                if (compute_cnt == 4'd7) acc[2] <= acc[2] + sa_res[2];
                if (compute_cnt == 4'd8) acc[3] <= acc[3] + sa_res[3];
            end

            // ---- Bulk weight load FSM ----
            if (bulk_active) begin
                case (bulk_phase)
                    2'd0: begin
                        sa_w_data <= bulk_data[15:8];
                        sa_w_col_sel <= 2'd1;
                        sa_w_row_sel <= bulk_row;
                        sa_load_weight <= 1'b1;
                        bulk_phase <= 2'd1;
                    end
                    2'd1: begin
                        sa_w_data <= bulk_data[23:16];
                        sa_w_col_sel <= 2'd2;
                        sa_w_row_sel <= bulk_row;
                        sa_load_weight <= 1'b1;
                        bulk_phase <= 2'd2;
                    end
                    2'd2: begin
                        sa_w_data <= bulk_data[31:24];
                        sa_w_col_sel <= 2'd3;
                        sa_w_row_sel <= bulk_row;
                        sa_load_weight <= 1'b1;
                        bulk_active <= 1'b0;
                        w_addr_row <= bulk_row + 2'd1;  // auto-increment for next W_DATA4
                    end
                    default: bulk_active <= 1'b0;
                endcase
            end else begin
                // Clear load_weight when not in bulk mode (and not LUT-load)
                if (sa_load_weight && !lut_load_active)
                    sa_load_weight <= 1'b0;
            end

            // ---- LUT->weight auto-load FSM (v3) ----
            // 2 cycles per LUT entry.  Phase 0: BRAM read addr presented
            // (handled combinationally by lut_rd_addr_mux); phase 1: data
            // is in lut_rdata_r, drive PE load_weight pulse + advance idx.
            if (lut_load_active) begin
                if (lut_load_phase == 1'b0) begin
                    // Address presented this cycle; LUT data will be in
                    // lut_rdata_r next cycle.  Nothing to drive yet.
                    lut_load_phase <= 1'b1;
                end else begin
                    // Data captured.  Drive PE weight load.  Use bits
                    // [23:16] of the LUT entry as the weight byte —
                    // matches mask[7:0] in the firmware packing
                    // convention so weights vary record-to-record.
                    sa_w_data    <= lut_rdata_r[23:16];
                    sa_w_col_sel <= lut_load_idx[1:0];
                    sa_w_row_sel <= lut_load_idx[3:2];
                    sa_load_weight <= 1'b1;
                    if (lut_load_idx == 5'd15) begin
                        lut_load_active <= 1'b0;
                    end else begin
                        lut_load_idx <= lut_load_idx + 5'd1;
                        lut_load_phase <= 1'b0;
                    end
                end
            end

            // Bus writes — gated on systolic region in v2 so LUT writes
            // don't accidentally hit the systolic register decode.  v3:
            // also blocked while LUT-load FSM is running.
            if (sel && (wstrb != 4'b0000) && !bulk_active && !lut_load_active && in_systolic) begin
                case (addr[5:2])  // word address (within systolic region only)
                    4'h0: begin // CTRL
                        if (wdata[0] && !computing) begin
                            // Start compute
                            compute_cnt <= 4'd1;
                            done <= 1'b0;
                        end
                        if (wdata[4]) begin
                            // Clear accumulators and done
                            acc[0] <= 32'sd0;
                            acc[1] <= 32'sd0;
                            acc[2] <= 32'sd0;
                            acc[3] <= 32'sd0;
                            done <= 1'b0;
                        end
                        if (wdata[8] && !computing) begin
                            // v3: trigger LUT->weight auto-load FSM.  Idle-only;
                            // the firmware should poll STATUS[1] until 0 before
                            // issuing CTRL[0]=1 to compute.
                            lut_load_active <= 1'b1;
                            lut_load_idx <= 5'd0;
                            lut_load_phase <= 1'b0;
                        end
                    end
                    4'h2: begin // W_ADDR
                        w_addr_col <= wdata[1:0];
                        w_addr_row <= wdata[3:2];
                    end
                    4'h3: begin // W_DATA — single weight, triggers load_weight pulse
                        sa_w_data <= wdata[7:0];
                        sa_w_row_sel <= w_addr_row;
                        sa_w_col_sel <= w_addr_col;
                        sa_load_weight <= 1'b1;
                    end
                    4'h4: begin // X_IN
                        x_in_reg <= wdata;
                    end
                    4'h5: begin // W_DATA4 — bulk load 4 weights for one row
                        // Load col 0 immediately
                        sa_w_data <= wdata[7:0];
                        sa_w_col_sel <= 2'd0;
                        sa_w_row_sel <= w_addr_row;
                        sa_load_weight <= 1'b1;
                        // Latch remaining data for FSM
                        bulk_data <= wdata;
                        bulk_row <= w_addr_row;
                        bulk_phase <= 2'd0;
                        bulk_active <= 1'b1;
                    end
                endcase
            end
        end
    end

    // ---- LUT memory (256 × 32-bit, M9K-backed) ------------------------------
    // Style: synchronous read + synchronous write, registered output.
    // Quartus 21.1 infers a single M9K block from this pattern.
    //
    // v3: read-address is muxed between the XBUS-derived lut_idx (when idle)
    // and the lut_load_idx FSM source (when CTRL[8]-triggered auto-load is
    // running).  Quartus still infers a single-port M9K because both
    // sources collapse into one combinational wire feeding the BRAM read.
    reg [31:0] lut_mem [0:255];
    reg [31:0] lut_rdata_r;
    wire [7:0] lut_rd_addr_mux = lut_load_active ? {4'd0, lut_load_idx[3:0]}
                                                  : lut_idx;

    always @(posedge clk) begin
        // Synchronous write — gated against auto-load FSM so the read port
        // isn't fighting an active write address.
        if (sel && (wstrb != 4'b0000) && in_lut && !bulk_active && !lut_load_active) begin
            lut_mem[lut_idx] <= wdata;
        end
        lut_rdata_r <= lut_mem[lut_rd_addr_mux];
    end

    // ---- Bus reads ----
    always @(*) begin
        if (in_lut) begin
            rdata = lut_rdata_r;
        end else begin
            case (addr[5:2])
                4'h1:    rdata = {30'd0, lut_load_active, done};   // STATUS [1]=lut_load_busy, [0]=done
                4'h8:    rdata = acc[0];                  // RES0
                4'h9:    rdata = acc[1];                  // RES1
                4'hA:    rdata = acc[2];                  // RES2
                4'hB:    rdata = acc[3];                  // RES3
                default: rdata = 32'd0;
            endcase
        end
    end

    // ---- Ready signal ----
    // 1-cycle latency for normal accesses; stall during bulk_active
    reg ready_r;
    always @(posedge clk or negedge rst_n)
        if (!rst_n) ready_r <= 1'b0;
        else        ready_r <= sel && !ready_r && !bulk_active;

    always @(*) ready = ready_r;

    assign debug_led = {done, computing, 2'b00};

endmodule
