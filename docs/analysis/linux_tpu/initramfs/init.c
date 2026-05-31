/* Minimal nommu init for RISC-V 32 — static PIE, no libc
 * For NEORV32 RV32IMAC nommu Linux on AX301 (EP4CE10) + 32MB SDRAM + TPU.
 *
 * Extends see_neorv32_run_linux's init with a `tpu` command: the
 * dynamic-load + trust-anchor -> useful-compute demo (Linux+TPU, candidate d).
 *
 * On nommu there is no MMU/PMP (PMP_NUM_REGIONS=0), so this U-mode userspace
 * pokes the TPU's MMIO registers at 0xF0000000 directly — the same mechanism
 * as diag_putc()'s direct UART write. The TPU (4x4 int8 weight-stationary
 * systolic array) computes RES[row] = sum_col W[row][col]*X[col] = a 4-input
 * -> 4-output quantized dense layer; argmax(RES) = predicted class.
 *
 * Trust anchor: the (trusted, in-initramfs) init FNV-1a-32 hashes the 16
 * model-weight bytes read from /tpu_model.bin and refuses to load them into
 * the fabric unless the hash matches the baked-in TPU_MODEL_HASH. Mutable
 * workload = the model file; trust gate = this hash check. Mirrors the RoT's
 * CRC/whitelist gate, one level up the stack.
 *
 * Build: see Makefile (riscv32-buildroot-linux-gnu-gcc, -nostdlib static-PIE).
 */

/* Golden constants from host_reference.py (single source of truth):
 *   model = 4x4 int8 {{40,30,20,10},{10,20,30,40},{50,-50,50,-50},{-50,50,-50,50}}
 *   query X = {1,2,3,4}; expected RES = {200,300,-100,100}; class 1. */
#define TPU_MODEL_HASH 0x9CA88565u   /* FNV-1a-32 of the 16 model bytes */
#define TPU_BASE       0xF0000000UL

static inline __attribute__((always_inline)) long
my_syscall(long n, long a0, long a1, long a2) {
    register long _a7 __asm__("a7") = n;
    register long _a0 __asm__("a0") = a0;
    register long _a1 __asm__("a1") = a1;
    register long _a2 __asm__("a2") = a2;
    __asm__ volatile("ecall" : "+r"(_a0) : "r"(_a1), "r"(_a2), "r"(_a7)
        : "memory", "t0", "t1", "t2", "t3", "t4", "t5", "t6");
    return _a0;
}
static inline __attribute__((always_inline)) long
my_syscall4(long n, long a0, long a1, long a2, long a3) {
    register long _a7 __asm__("a7") = n;
    register long _a0 __asm__("a0") = a0;
    register long _a1 __asm__("a1") = a1;
    register long _a2 __asm__("a2") = a2;
    register long _a3 __asm__("a3") = a3;
    __asm__ volatile("ecall" : "+r"(_a0) : "r"(_a1), "r"(_a2), "r"(_a3), "r"(_a7)
        : "memory", "t0", "t1", "t2", "t3", "t4", "t5", "t6");
    return _a0;
}

/* syscall numbers (RISC-V 32, generic table) */
#define __NR_exit       93
#define __NR_read       63
#define __NR_write      64
#define __NR_openat     56
#define __NR_close      57
#define __NR_uname      160
#define __NR_sysinfo    179
#define AT_FDCWD        (-100)
#define O_RDONLY        0

struct utsname {
    char sysname[65]; char nodename[65]; char release[65];
    char version[65]; char machine[65];  char domainname[65];
};
struct sysinfo {
    long uptime; unsigned long loads[3];
    unsigned long totalram, freeram, sharedram, bufferram, totalswap, freeswap;
    unsigned short procs, pad;
    unsigned long totalhigh, freehigh; unsigned int mem_unit; char _f[8];
};

static int my_strlen(const char *s) { int n=0; while(s[n])n++; return n; }
static void my_puts(const char *s) { my_syscall(__NR_write, 1, (long)s, my_strlen(s)); }

static void my_putnum(unsigned long v) {
    char buf[12]; int i = 11; buf[i] = 0;
    if (v == 0) { my_puts("0"); return; }
    do { buf[--i] = '0' + (v % 10); v /= 10; } while (v);
    my_puts(buf + i);
}
static void my_putint(int v) {
    if (v < 0) { my_puts("-"); my_putnum((unsigned long)(-(long)v)); }
    else my_putnum((unsigned long)v);
}
static void my_puthex(unsigned long v) {
    char buf[12]; int i; buf[0]='0'; buf[1]='x';
    for (i = 0; i < 8; i++) buf[2+i] = "0123456789abcdef"[(v >> (28 - i*4)) & 0xf];
    buf[10] = 0; my_puts(buf);
}
static int my_strcmp(const char *a, const char *b) {
    while (*a && *a == *b) { a++; b++; } return *a - *b;
}
static void chomp(char *s) {
    int n = my_strlen(s);
    while (n > 0 && (s[n-1] == '\n' || s[n-1] == '\r')) s[--n] = 0;
}

static void cmd_uname(void) {
    struct utsname u;
    if (my_syscall(__NR_uname, (long)&u, 0, 0) == 0) {
        my_puts(u.sysname); my_puts(" "); my_puts(u.nodename); my_puts(" ");
        my_puts(u.release); my_puts(" "); my_puts(u.machine); my_puts("\n");
    }
}
static void cmd_info(void) {
    struct sysinfo si;
    if (my_syscall(__NR_sysinfo, (long)&si, 0, 0) == 0) {
        unsigned long unit = si.mem_unit ? si.mem_unit : 1;
        my_puts("Uptime:    "); my_putnum(si.uptime); my_puts(" s\n");
        my_puts("Total RAM: "); my_putnum((si.totalram * unit) >> 10); my_puts(" KB\n");
        my_puts("Free RAM:  "); my_putnum((si.freeram * unit) >> 10); my_puts(" KB\n");
    }
}

/* ── TPU dense-layer classifier (the Linux+TPU demo) ─────────────────────────
 * Direct U-mode MMIO at 0xF0000000 (nommu, no PMP). Register map:
 *   tpu[0]=CTRL [0]=start [4]=clear   tpu[1]=STATUS [0]=done
 *   tpu[2]=W_ADDR [1:0]=col [3:2]=row tpu[3]=W_DATA [7:0]=signed weight
 *   tpu[4]=X_IN {x3,x2,x1,x0} int8    tpu[8..11]=RES0-3 (int32) */
static void cmd_tpu(void) {
    volatile unsigned int *tpu = (volatile unsigned int *)TPU_BASE;
    unsigned char buf[20];
    int fd, n, i, res[4], best, bestval;
    unsigned int h;

    my_puts("[tpu] reading workload /tpu_model.bin ...\n");
    fd = my_syscall4(__NR_openat, AT_FDCWD, (long)"/tpu_model.bin", O_RDONLY, 0);
    if (fd < 0) { my_puts("[tpu] ERROR: cannot open /tpu_model.bin\n"); return; }
    n = my_syscall(__NR_read, fd, (long)buf, 20);
    my_syscall(__NR_close, fd, 0, 0);
    if (n != 20) { my_puts("[tpu] ERROR: model file must be 20 bytes (16 W + 4 X)\n"); return; }

    /* trust gate: FNV-1a-32 over the 16 model bytes vs the baked-in anchor */
    h = 0x811C9DC5u;
    for (i = 0; i < 16; i++) { h ^= buf[i]; h *= 0x01000193u; }
    my_puts("[tpu] model hash = "); my_puthex(h);
    my_puts("  anchor = "); my_puthex(TPU_MODEL_HASH); my_puts("\n");
    if (h != TPU_MODEL_HASH) {
        my_puts("[tpu] TRUST FAIL: model hash != anchor — refusing to load fabric.\n");
        return;
    }
    my_puts("[tpu] trust OK; programming TPU weights (4x4 int8).\n");

    tpu[0] = 0x10u;                          /* clear accumulators */
    for (i = 0; i < 16; i++) {               /* W_ADDR=(row<<2)|col, then W_DATA */
        tpu[2] = (unsigned)(((i >> 2) << 2) | (i & 3));
        tpu[3] = (unsigned)buf[i];           /* low 8 bits = signed weight */
    }
    tpu[4] = ((unsigned)buf[19] << 24) | ((unsigned)buf[18] << 16)
           | ((unsigned)buf[17] << 8)  |  (unsigned)buf[16];   /* X_IN */
    tpu[0] = 0x1u;                           /* start compute */

    { unsigned int to = 2000000u;
      while (!(tpu[1] & 1u)) { if (--to == 0) { my_puts("[tpu] compute timeout\n"); return; } } }

    best = 0; bestval = (int)tpu[8];
    for (i = 0; i < 4; i++) {
        res[i] = (int)tpu[8 + i];
        my_puts("  RES["); my_putnum(i); my_puts("] = "); my_putint(res[i]); my_puts("\n");
        if (res[i] > bestval) { bestval = res[i]; best = i; }
    }
    my_puts("[tpu] >>> predicted class = "); my_putnum(best);
    my_puts("  (score "); my_putint(bestval);
    my_puts(")  [expect class 1, score 300]\n");
}

static void cmd_help(void) {
    my_puts("Commands: uname | info | tpu | help | exit\n");
    my_puts("  tpu - run the TPU dense-layer classifier (trust-gated)\n");
}

/* Direct UART poke (nommu, no MMU) — proof of life before syscalls. */
static inline void __attribute__((always_inline)) diag_putc(char c) {
    volatile unsigned int *uart = (volatile unsigned int *)0xFFF50000UL;
    while (!(uart[0] & (1u << 19))) ;
    uart[1] = (unsigned int)(unsigned char)c;
}

void _start(void) __attribute__((section(".text.init")));
void _start(void) {
    char buf[128];
    int n;

    diag_putc('!');  /* tiny direct-UART proof we started */

    my_puts("\n========================================\n");
    my_puts(" NEORV32 nommu Linux + TPU — mini shell \n");
    my_puts("========================================\n");
    cmd_uname();
    cmd_info();

    /* Auto-run the Linux+TPU demo once at boot. */
    my_puts("\n--- Linux+TPU dense-layer classifier (auto) ---\n");
    cmd_tpu();
    my_puts("-----------------------------------------------\n");

    my_puts("\nType 'help' for commands.\n\n");
    for (;;) {
        my_puts("nommu# ");
        n = my_syscall(__NR_read, 0, (long)buf, 127);
        if (n <= 0) break;
        buf[n] = 0; chomp(buf);
        if (buf[0] == 0) continue;
        if      (my_strcmp(buf, "uname") == 0) cmd_uname();
        else if (my_strcmp(buf, "info")  == 0) cmd_info();
        else if (my_strcmp(buf, "tpu")   == 0) cmd_tpu();
        else if (my_strcmp(buf, "help")  == 0) cmd_help();
        else if (my_strcmp(buf, "exit")  == 0) break;
        else { my_puts("unknown: "); my_puts(buf); my_puts("\n"); }
    }
    my_puts("Halting.\n");
    my_syscall(__NR_exit, 0, 0, 0);
}
