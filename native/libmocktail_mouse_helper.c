#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <signal.h>
#include <time.h>
#include <unistd.h>
#include <GLES3/gl3.h>
#include <SDL3/SDL.h>

// =============================================================================
// Mocktail Text Overlay ABI (56 bytes layout)
// =============================================================================
struct MocktailTextOverlayFrameInfo {
    uint32_t version;     // 0x00: expected 2
    uint32_t active;      // 0x04: 1 if active, 0 otherwise
    uint64_t generation;  // 0x08: generation counter
    int32_t viewport_w;   // 0x10: coordinate space width
    int32_t viewport_h;   // 0x14: coordinate space height
    int32_t x;            // 0x18: text bitmap left
    int32_t y;            // 0x1c: text bitmap top
    int32_t bitmap_w;     // 0x20: rasterized bitmap width
    int32_t bitmap_h;     // 0x24: rasterized bitmap height
    int32_t pitch;        // 0x28: row pitch in bytes (bitmap_w * 4)
    uint32_t pad;         // 0x2c
    uint64_t data_size;   // 0x30: pitch * bitmap_h
};

static int (*real_mocktail_text_overlay_query)(struct MocktailTextOverlayFrameInfo *info) = NULL;
static int (*real_mocktail_text_overlay_copy)(uint64_t generation, void *buf, size_t size) = NULL;

// =============================================================================
// SDL3 Function Pointers
// =============================================================================
static bool (*real_SDL_SetWindowRelativeMouseMode)(SDL_Window *window, bool enabled) = NULL;
static void (*real_SDL_WarpMouseInWindow)(SDL_Window *window, float x, float y) = NULL;
static bool (*real_SDL_GetWindowSize)(SDL_Window *window, int *w, int *h) = NULL;
static bool (*real_SDL_GetWindowSizeInPixels)(SDL_Window *window, int *w, int *h) = NULL;
static SDL_MouseButtonFlags (*real_SDL_GetMouseState)(float *x, float *y) = NULL;
static bool (*real_SDL_ShowCursor)(void) = NULL;
static bool (*real_SDL_HideCursor)(void) = NULL;
static bool (*real_SDL_AddEventWatch)(SDL_EventFilter filter, void *userdata) = NULL;
static int (*real_SDL_PeepEvents)(SDL_Event *events, int numevents, SDL_EventAction action, Uint32 minType, Uint32 maxType) = NULL;
static bool (*real_SDL_GL_SwapWindow)(SDL_Window *window) = NULL;
static bool (*real_SDL_GL_SetSwapInterval)(int interval) = NULL;
static bool (*real_SDL_PushEvent)(SDL_Event *event) = NULL;
static Uint64 (*real_SDL_GetTicksNS)(void) = NULL;
static SDL_WindowID (*real_SDL_GetWindowID)(SDL_Window *window) = NULL;

static volatile sig_atomic_t g_afk_pulse_requested = 0;
static uint64_t last_user_input_time = 0;
static time_t last_shm_pulse_time = 0;

static void handle_sigusr1(int sig) {
    (void)sig;
    g_afk_pulse_requested = 1;
}

// Optional VAO function pointers
static void (*pfn_glGenVertexArrays)(GLsizei, GLuint *) = NULL;
static void (*pfn_glBindVertexArray)(GLuint) = NULL;
static bool vao_funcs_initialized = false;

static SDL_Window *last_window = NULL;
static bool event_watch_registered = false;
static bool button_drag_active = false;
static float drag_start_x = 0.0f;
static float drag_start_y = 0.0f;
static uint64_t last_button_down_time = 0;
static uint64_t last_button_up_time = 0;
static uint64_t last_shift_key_time = 0;

static uint64_t get_time_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)(ts.tv_nsec / 1000000ULL);
}

// =============================================================================
// Shared Memory Typing Flag for Macro (/dev/shm/roblox_typing_active)
// =============================================================================
static bool this_instance_typing = false;

static void update_typing_shm(bool active) {
    this_instance_typing = active;
    static int shm_fd = -1;
    static volatile uint8_t *shm_ptr = NULL;
    if (shm_fd < 0) {
        shm_fd = open("/dev/shm/roblox_typing_active", O_RDWR | O_CREAT, 0666);
        if (shm_fd >= 0) {
            fchmod(shm_fd, 0666);
            if (ftruncate(shm_fd, 4) == 0) {
                shm_ptr = (volatile uint8_t *)mmap(NULL, 4, PROT_READ | PROT_WRITE, MAP_SHARED, shm_fd, 0);
                if (shm_ptr == MAP_FAILED) shm_ptr = NULL;
            }
        }
    }
    if (shm_ptr) {
        *shm_ptr = active ? 1 : 0;
    }
}

__attribute__((destructor))
static void cleanup_helper(void) {
    update_typing_shm(false);
}

// =============================================================================
// SDL Event Watcher (Right-Click Drag & Shift Lock Tracking)
// =============================================================================
static bool SDLCALL event_watcher(void *userdata, SDL_Event *event) {
    (void)userdata;
    if (!event) return true;

    // Track any physical/user input for anti-afk idle awareness
    if (event->type == SDL_EVENT_MOUSE_MOTION ||
        event->type == SDL_EVENT_MOUSE_BUTTON_DOWN ||
        event->type == SDL_EVENT_MOUSE_BUTTON_UP ||
        event->type == SDL_EVENT_KEY_DOWN ||
        event->type == SDL_EVENT_KEY_UP) {
        last_user_input_time = get_time_ms();
    }

    if (event->type == SDL_EVENT_MOUSE_BUTTON_DOWN) {
        if (event->button.button == SDL_BUTTON_RIGHT || event->button.button == SDL_BUTTON_MIDDLE) {
            button_drag_active = true;
            drag_start_x = event->button.x;
            drag_start_y = event->button.y;
            last_button_down_time = get_time_ms();
        }
    } else if (event->type == SDL_EVENT_MOUSE_BUTTON_UP) {
        if (event->button.button == SDL_BUTTON_RIGHT || event->button.button == SDL_BUTTON_MIDDLE) {
            button_drag_active = false;
            last_button_up_time = get_time_ms();
        }
    } else if (event->type == SDL_EVENT_KEY_DOWN || event->type == SDL_EVENT_KEY_UP) {
        if (event->key.scancode == SDL_SCANCODE_LSHIFT || event->key.scancode == SDL_SCANCODE_RSHIFT) {
            last_shift_key_time = get_time_ms();
        }
    }
    return true;
}

// In-process Anti-AFK event injection (Affects ONLY this Mocktail process, ZERO focus stealing)
static void inject_anti_afk_event(SDL_Window *window) {
    if (!real_SDL_PushEvent) {
        real_SDL_PushEvent = (bool (*)(SDL_Event *))dlsym(RTLD_DEFAULT, "SDL_PushEvent");
        if (!real_SDL_PushEvent) return;
    }
    if (!real_SDL_GetTicksNS) {
        real_SDL_GetTicksNS = (Uint64 (*)(void))dlsym(RTLD_DEFAULT, "SDL_GetTicksNS");
    }
    if (!real_SDL_GetWindowID) {
        real_SDL_GetWindowID = (SDL_WindowID (*)(SDL_Window *))dlsym(RTLD_DEFAULT, "SDL_GetWindowID");
    }

    Uint64 t = real_SDL_GetTicksNS ? real_SDL_GetTicksNS() : (Uint64)get_time_ms() * 1000000ULL;
    SDL_WindowID wid = (real_SDL_GetWindowID && window) ? real_SDL_GetWindowID(window) : 0;

    // 1. Mouse motion jitter to reset Roblox UserInputService idle timer
    SDL_Event motion1;
    memset(&motion1, 0, sizeof(motion1));
    motion1.type = SDL_EVENT_MOUSE_MOTION;
    motion1.motion.timestamp = t;
    motion1.motion.windowID = wid;
    motion1.motion.xrel = 1.0f;
    motion1.motion.yrel = 0.0f;
    real_SDL_PushEvent(&motion1);

    SDL_Event motion2;
    memset(&motion2, 0, sizeof(motion2));
    motion2.type = SDL_EVENT_MOUSE_MOTION;
    motion2.motion.timestamp = t + 5000000ULL;
    motion2.motion.windowID = wid;
    motion2.motion.xrel = -1.0f;
    motion2.motion.yrel = 0.0f;
    real_SDL_PushEvent(&motion2);

    // 2. Benign jump key tap (SPACE) to keep game server activity state fresh
    SDL_Event ev_down;
    memset(&ev_down, 0, sizeof(ev_down));
    ev_down.type = SDL_EVENT_KEY_DOWN;
    ev_down.key.timestamp = t + 10000000ULL;
    ev_down.key.windowID = wid;
    ev_down.key.scancode = SDL_SCANCODE_SPACE;
    ev_down.key.key = SDLK_SPACE;
    ev_down.key.down = true;
    real_SDL_PushEvent(&ev_down);

    SDL_Event ev_up;
    memset(&ev_up, 0, sizeof(ev_up));
    ev_up.type = SDL_EVENT_KEY_UP;
    ev_up.key.timestamp = t + 30000000ULL;
    ev_up.key.windowID = wid;
    ev_up.key.scancode = SDL_SCANCODE_SPACE;
    ev_up.key.key = SDLK_SPACE;
    ev_up.key.down = false;
    real_SDL_PushEvent(&ev_up);
}

static FILE *dbg_fp = NULL;
static int dbg_checked = 0;
#define DBG(...) do { \
    if (!dbg_checked) { \
        dbg_checked = 1; \
        if (getenv("AERAM_OVERLAY_DEBUG")) dbg_fp = fopen("/tmp/mocktail_overlay_debug.log", "a"); \
    } \
    if (dbg_fp) { fprintf(dbg_fp, __VA_ARGS__); fflush(dbg_fp); } \
} while (0)

// =============================================================================
// Mocktail Text Overlay Geometry: Kept native to prevent binary stack corruption
// =============================================================================

// =============================================================================
// Framerate Uncapping (Eliminates hardcoded 240 FPS ceilings in Mocktail & libroblox.so)
// =============================================================================
static bool unclamp_process_framerate(void) {
    FILE *f = fopen("/proc/self/maps", "r");
    if (!f) return false;

    char line[512];
    static bool mocktail_patched = false;
    static bool libroblox_patched = false;

    // Pattern 1 in libroblox.so (GetTaskSchedulerTargetFps & RenderScheduler clamps):
    // mov ecx, 240; cmp eax, ecx; cmovl ecx, eax -> b9 f0 00 00 00 39 c8 0f 4c c8
    const uint8_t pat_roblox1[] = { 0xb9, 0xf0, 0x00, 0x00, 0x00, 0x39, 0xc8, 0x0f, 0x4c, 0xc8 };
    // Pattern 2 in libroblox.so (nativePostClientSettingsLoadedInitialization3):
    // mov ecx, 240; cmp eax, ecx; cmovae eax, ecx -> b9 f0 00 00 00 39 c8 0f 43 c1
    const uint8_t pat_roblox2[] = { 0xb9, 0xf0, 0x00, 0x00, 0x00, 0x39, 0xc8, 0x0f, 0x43, 0xc1 };
    // Pattern 3 in mocktail (MergeFrameRateClientSettingsOverrides mode 3 'unlimited'):
    // mov esi, 240; mov rdi, r12 -> be f0 00 00 00 4c 89 e7
    const uint8_t pat_mocktail[] = { 0xbe, 0xf0, 0x00, 0x00, 0x00, 0x4c, 0x89, 0xe7 };

    // Set engine ceiling to 10,000 FPS so any preset (60, 120, 144, 165, 240, 320, unlimited) runs uninhibited
    const uint32_t uncapped_limit = 10000;
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) page_size = 4096;

    while (fgets(line, sizeof(line), f)) {
        bool is_mocktail = (!mocktail_patched && strstr(line, "mocktail") && !strstr(line, "libmocktail_mouse_helper") && !strstr(line, ".log"));
        bool is_roblox = (!libroblox_patched && strstr(line, "libroblox.so"));

        if (!is_mocktail && !is_roblox) continue;

        uintptr_t start = 0, end = 0;
        char perms[16];
        if (sscanf(line, "%lx-%lx %15s", &start, &end, perms) != 3) continue;
        if (!strchr(perms, 'r')) continue;

        size_t len = end - start;
        uint8_t *mem = (uint8_t *)start;

        if (is_mocktail && strchr(perms, 'x')) {
            for (size_t i = 0; i + sizeof(pat_mocktail) <= len; i++) {
                if (memcmp(mem + i, pat_mocktail, sizeof(pat_mocktail)) == 0) {
                    uintptr_t page_start = (uintptr_t)(mem + i) & ~(page_size - 1);
                    uintptr_t page_end = ((uintptr_t)(mem + i + sizeof(pat_mocktail)) + page_size - 1) & ~(page_size - 1);
                    size_t psize = page_end - page_start;
                    mprotect((void *)page_start, psize, PROT_READ | PROT_WRITE | PROT_EXEC);
                    memcpy(mem + i + 1, &uncapped_limit, sizeof(uncapped_limit));
                    if (!strchr(perms, 'w')) {
                        mprotect((void *)page_start, psize, PROT_READ | PROT_EXEC);
                    }
                    mocktail_patched = true;
                    fprintf(stderr, "[AERAM-FPS] Unlocked Mocktail 'unlimited' hardcode at %p to %u FPS\n", (void *)(mem + i), uncapped_limit);
                    break;
                }
            }
        }

        if (is_roblox && strchr(perms, 'x')) {
            int patches_applied = 0;
            for (size_t i = 0; i + 10 <= len; i++) {
                if (memcmp(mem + i, pat_roblox1, sizeof(pat_roblox1)) == 0 ||
                    memcmp(mem + i, pat_roblox2, sizeof(pat_roblox2)) == 0) {
                    uintptr_t page_start = (uintptr_t)(mem + i) & ~(page_size - 1);
                    uintptr_t page_end = ((uintptr_t)(mem + i + 10) + page_size - 1) & ~(page_size - 1);
                    size_t psize = page_end - page_start;
                    mprotect((void *)page_start, psize, PROT_READ | PROT_WRITE | PROT_EXEC);
                    memcpy(mem + i + 1, &uncapped_limit, sizeof(uncapped_limit));
                    if (!strchr(perms, 'w')) {
                        mprotect((void *)page_start, psize, PROT_READ | PROT_EXEC);
                    }
                    patches_applied++;
                    fprintf(stderr, "[AERAM-FPS] Unlocked libroblox.so 240 FPS clamp #%d at %p to %u FPS\n", patches_applied, (void *)(mem + i), uncapped_limit);
                }
            }
            if (patches_applied >= 4) {
                libroblox_patched = true;
            }
        }
    }
    fclose(f);
    return libroblox_patched;
}

__attribute__((constructor))
static void ctor_init(void) {
    unclamp_process_framerate();
    last_user_input_time = get_time_ms();

    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = handle_sigusr1;
    sa.sa_flags = SA_RESTART;
    sigaction(SIGUSR1, &sa, NULL);
}

static void init_symbols(void) {
    if (!real_SDL_SetWindowRelativeMouseMode) {
        real_SDL_SetWindowRelativeMouseMode = (bool (*)(SDL_Window *, bool))dlsym(RTLD_NEXT, "SDL_SetWindowRelativeMouseMode");
    }
    if (!real_SDL_WarpMouseInWindow) {
        real_SDL_WarpMouseInWindow = (void (*)(SDL_Window *, float, float))dlsym(RTLD_NEXT, "SDL_WarpMouseInWindow");
    }
    if (!real_SDL_GetWindowSize) {
        real_SDL_GetWindowSize = (bool (*)(SDL_Window *, int *, int *))dlsym(RTLD_NEXT, "SDL_GetWindowSize");
    }
    if (!real_SDL_GetWindowSizeInPixels) {
        real_SDL_GetWindowSizeInPixels = (bool (*)(SDL_Window *, int *, int *))dlsym(RTLD_NEXT, "SDL_GetWindowSizeInPixels");
    }
    if (!real_SDL_GetMouseState) {
        real_SDL_GetMouseState = (SDL_MouseButtonFlags (*)(float *, float *))dlsym(RTLD_NEXT, "SDL_GetMouseState");
    }
    if (!real_SDL_ShowCursor) {
        real_SDL_ShowCursor = (bool (*)(void))dlsym(RTLD_NEXT, "SDL_ShowCursor");
    }
    if (!real_SDL_HideCursor) {
        real_SDL_HideCursor = (bool (*)(void))dlsym(RTLD_NEXT, "SDL_HideCursor");
    }
    if (!real_SDL_AddEventWatch) {
        real_SDL_AddEventWatch = (bool (*)(SDL_EventFilter, void *))dlsym(RTLD_NEXT, "SDL_AddEventWatch");
    }
    if (!real_SDL_PeepEvents) {
        real_SDL_PeepEvents = (int (*)(SDL_Event *, int, SDL_EventAction, Uint32, Uint32))dlsym(RTLD_NEXT, "SDL_PeepEvents");
    }
    if (!real_SDL_GL_SwapWindow) {
        real_SDL_GL_SwapWindow = (bool (*)(SDL_Window *))dlsym(RTLD_NEXT, "SDL_GL_SwapWindow");
    }
    if (!real_SDL_GL_SetSwapInterval) {
        real_SDL_GL_SetSwapInterval = (bool (*)(int))dlsym(RTLD_NEXT, "SDL_GL_SetSwapInterval");
    }
    if (!real_mocktail_text_overlay_query) {
        real_mocktail_text_overlay_query = (int (*)(struct MocktailTextOverlayFrameInfo *))dlsym(RTLD_DEFAULT, "mocktail_text_overlay_query");
    }
    if (!real_mocktail_text_overlay_copy) {
        real_mocktail_text_overlay_copy = (int (*)(uint64_t, void *, size_t))dlsym(RTLD_DEFAULT, "mocktail_text_overlay_copy");
    }

    if (real_SDL_AddEventWatch && !event_watch_registered) {
        if (real_SDL_AddEventWatch(event_watcher, NULL)) {
            event_watch_registered = true;
        }
    }
}

static void init_vao_funcs(void) {
    if (vao_funcs_initialized) return;
    vao_funcs_initialized = true;

    pfn_glGenVertexArrays = (void (*)(GLsizei, GLuint *))SDL_GL_GetProcAddress("glGenVertexArrays");
    if (!pfn_glGenVertexArrays) {
        pfn_glGenVertexArrays = (void (*)(GLsizei, GLuint *))SDL_GL_GetProcAddress("glGenVertexArraysOES");
    }
    pfn_glBindVertexArray = (void (*)(GLuint))SDL_GL_GetProcAddress("glBindVertexArray");
    if (!pfn_glBindVertexArray) {
        pfn_glBindVertexArray = (void (*)(GLuint))SDL_GL_GetProcAddress("glBindVertexArrayOES");
    }
}

// =============================================================================
// OpenGL Text Overlay Compositor
// =============================================================================
static GLuint overlay_prog = 0;
static GLuint overlay_tex = 0;
static GLuint overlay_vbo = 0;
static GLuint overlay_vao = 0;
static GLint loc_pos = -1;
static GLint loc_uv = -1;
static GLint loc_tex = -1;
static uint64_t last_rendered_gen = 0;
static void *overlay_buf = NULL;
static size_t overlay_buf_cap = 0;
static bool shader_compile_failed = false;

static const char *vert_shader_source =
    "#ifdef GL_ES\n"
    "precision mediump float;\n"
    "#endif\n"
    "attribute vec2 a_pos;\n"
    "attribute vec2 a_uv;\n"
    "varying vec2 v_uv;\n"
    "void main() {\n"
    "    gl_Position = vec4(a_pos, 0.0, 1.0);\n"
    "    v_uv = a_uv;\n"
    "}\n";

static const char *frag_shader_source =
    "#ifdef GL_ES\n"
    "precision mediump float;\n"
    "#endif\n"
    "varying vec2 v_uv;\n"
    "uniform sampler2D u_tex;\n"
    "void main() {\n"
    "    gl_FragColor = texture2D(u_tex, v_uv);\n"
    "}\n";

static GLuint compile_shader(GLenum type, const char *source) {
    GLuint s = glCreateShader(type);
    if (!s) return 0;
    glShaderSource(s, 1, &source, NULL);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        glDeleteShader(s);
        return 0;
    }
    return s;
}

static bool init_overlay_resources(void) {
    if (overlay_prog) return true;
    if (shader_compile_failed) return false;

    GLuint vs = compile_shader(GL_VERTEX_SHADER, vert_shader_source);
    GLuint fs = compile_shader(GL_FRAGMENT_SHADER, frag_shader_source);
    if (!vs || !fs) {
        if (vs) glDeleteShader(vs);
        if (fs) glDeleteShader(fs);
        shader_compile_failed = true;
        return false;
    }

    overlay_prog = glCreateProgram();
    glAttachShader(overlay_prog, vs);
    glAttachShader(overlay_prog, fs);
    glLinkProgram(overlay_prog);

    glDeleteShader(vs);
    glDeleteShader(fs);

    GLint linked = 0;
    glGetProgramiv(overlay_prog, GL_LINK_STATUS, &linked);
    if (!linked) {
        glDeleteProgram(overlay_prog);
        overlay_prog = 0;
        shader_compile_failed = true;
        return false;
    }

    loc_pos = glGetAttribLocation(overlay_prog, "a_pos");
    loc_uv = glGetAttribLocation(overlay_prog, "a_uv");
    loc_tex = glGetUniformLocation(overlay_prog, "u_tex");

    glGenTextures(1, &overlay_tex);
    glGenBuffers(1, &overlay_vbo);

    init_vao_funcs();
    if (pfn_glGenVertexArrays) {
        pfn_glGenVertexArrays(1, &overlay_vao);
    }

    return true;
}

static unsigned long dbg_swaps = 0;
static int dbg_last_res = -99, dbg_last_active = -99;
static uint64_t dbg_last_gen = 0;

static void render_text_overlay(SDL_Window *window) {
    dbg_swaps++;
    if (dbg_swaps == 1 || dbg_swaps % 3000 == 0)
        DBG("[swap] count=%lu query=%p copy=%p tid=%ld\n", dbg_swaps,
            (void *)real_mocktail_text_overlay_query, (void *)real_mocktail_text_overlay_copy, (long)gettid());
    if (!real_mocktail_text_overlay_query || !real_mocktail_text_overlay_copy) {
        update_typing_shm(false);
        return;
    }

    struct MocktailTextOverlayFrameInfo info;
    memset(&info, 0, sizeof(info));
    info.version = 2;

    int res = real_mocktail_text_overlay_query(&info);
    if (res != dbg_last_res || (int)info.active != dbg_last_active || info.generation != dbg_last_gen) {
        DBG("[query] res=%d ver=%u active=%u gen=%llu viewport=%dx%d pos=%d,%d bitmap=%dx%d pitch=%d size=%llu\n",
            res, info.version, info.active, (unsigned long long)info.generation, info.viewport_w, info.viewport_h,
            info.x, info.y, info.bitmap_w, info.bitmap_h, info.pitch,
            (unsigned long long)info.data_size);
        dbg_last_res = res; dbg_last_active = (int)info.active; dbg_last_gen = info.generation;
    }
    if (res != 1 || info.active != 1 || info.bitmap_w <= 0 || info.bitmap_h <= 0 || info.data_size == 0) {
        update_typing_shm(false);
        return;
    }

    // Inform macro tool that textbox is active
    update_typing_shm(true);

    int win_w = 0, win_h = 0;
    if (real_SDL_GetWindowSizeInPixels) {
        real_SDL_GetWindowSizeInPixels(window, &win_w, &win_h);
    }
    if (win_w <= 0 || win_h <= 0) {
        if (real_SDL_GetWindowSize) {
            real_SDL_GetWindowSize(window, &win_w, &win_h);
        }
    }
    if (win_w <= 0 || win_h <= 0) return;

    if (!init_overlay_resources()) { static int once = 0; if (!once++) DBG("[init] overlay GL resources FAILED\n"); return; }

    // Save full GL state
    GLint last_viewport[4];
    glGetIntegerv(GL_VIEWPORT, last_viewport);

    GLint last_program = 0;
    glGetIntegerv(GL_CURRENT_PROGRAM, &last_program);

    GLint last_active_texture = 0;
    glGetIntegerv(GL_ACTIVE_TEXTURE, &last_active_texture);

    GLint last_texture = 0;
    glGetIntegerv(GL_TEXTURE_BINDING_2D, &last_texture);

    GLint last_array_buffer = 0;
    glGetIntegerv(GL_ARRAY_BUFFER_BINDING, &last_array_buffer);

    GLint last_vao = 0;
    if (pfn_glBindVertexArray) {
        glGetIntegerv(GL_VERTEX_ARRAY_BINDING, &last_vao);
        pfn_glBindVertexArray(overlay_vao ? overlay_vao : 0);
    }

    GLint last_fbo = 0;
    glGetIntegerv(GL_FRAMEBUFFER_BINDING, &last_fbo);
    if (last_fbo != 0) {
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
    }

    GLboolean last_blend = glIsEnabled(GL_BLEND);
    GLint last_blend_src_rgb = GL_ONE, last_blend_dst_rgb = GL_ZERO;
    GLint last_blend_src_alpha = GL_ONE, last_blend_dst_alpha = GL_ZERO;
    glGetIntegerv(GL_BLEND_SRC_RGB, &last_blend_src_rgb);
    glGetIntegerv(GL_BLEND_DST_RGB, &last_blend_dst_rgb);
    glGetIntegerv(GL_BLEND_SRC_ALPHA, &last_blend_src_alpha);
    glGetIntegerv(GL_BLEND_DST_ALPHA, &last_blend_dst_alpha);

    GLboolean last_depth_test = glIsEnabled(GL_DEPTH_TEST);
    GLboolean last_cull_face = glIsEnabled(GL_CULL_FACE);
    GLboolean last_scissor_test = glIsEnabled(GL_SCISSOR_TEST);

    GLboolean last_color_mask[4];
    glGetBooleanv(GL_COLOR_WRITEMASK, last_color_mask);

    // Setup rendering state
    glViewport(0, 0, win_w, win_h);
    glEnable(GL_BLEND);
    glBlendFuncSeparate(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA, GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_CULL_FACE);
    glDisable(GL_SCISSOR_TEST);
    glColorMask(GL_TRUE, GL_TRUE, GL_TRUE, GL_TRUE);

    // Upload texture bitmap if generation changed
    if (info.generation != last_rendered_gen || overlay_tex == 0) {
        if (info.data_size > overlay_buf_cap) {
            free(overlay_buf);
            overlay_buf = malloc(info.data_size);
            overlay_buf_cap = overlay_buf ? info.data_size : 0;
        }
        if (overlay_buf && real_mocktail_text_overlay_copy(info.generation, overlay_buf, info.data_size)) {
            size_t min_req = (size_t)info.bitmap_w * (size_t)info.bitmap_h * 4;
            if (info.data_size >= min_req && info.bitmap_w > 0 && info.bitmap_h > 0) {
                glActiveTexture(GL_TEXTURE0);
                glBindTexture(GL_TEXTURE_2D, overlay_tex);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
                glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
                glPixelStorei(GL_UNPACK_ALIGNMENT, 4);
                glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, info.bitmap_w, info.bitmap_h, 0, GL_RGBA, GL_UNSIGNED_BYTE, overlay_buf);
                last_rendered_gen = info.generation;
            }
        }
    } else {
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_2D, overlay_tex);
    }

    // Geometry: position/size are in Mocktail's viewport space; map to window pixels.
    float sx = (info.viewport_w > 0) ? (float)win_w / (float)info.viewport_w : 1.0f;
    float sy = (info.viewport_h > 0) ? (float)win_h / (float)info.viewport_h : 1.0f;

    float x0 = (float)info.x * sx;
    float y0 = (float)info.y * sy;
    float x1 = x0 + (float)info.bitmap_w * sx;
    float y1 = y0 + (float)info.bitmap_h * sy;

    float ndc_x0 = (2.0f * x0 / (float)win_w) - 1.0f;
    float ndc_y0 = 1.0f - (2.0f * y0 / (float)win_h);
    float ndc_x1 = (2.0f * x1 / (float)win_w) - 1.0f;
    float ndc_y1 = 1.0f - (2.0f * y1 / (float)win_h);

    float quad_verts[16] = {
        // x, y, u, v
        ndc_x0, ndc_y0, 0.0f, 0.0f, // Top-Left
        ndc_x0, ndc_y1, 0.0f, 1.0f, // Bottom-Left
        ndc_x1, ndc_y0, 1.0f, 0.0f, // Top-Right
        ndc_x1, ndc_y1, 1.0f, 1.0f  // Bottom-Right
    };

    glBindBuffer(GL_ARRAY_BUFFER, overlay_vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof(quad_verts), quad_verts, GL_STREAM_DRAW);

    glUseProgram(overlay_prog);
    glUniform1i(loc_tex, 0);

    glEnableVertexAttribArray(loc_pos);
    glEnableVertexAttribArray(loc_uv);

    glVertexAttribPointer(loc_pos, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), (void *)0);
    glVertexAttribPointer(loc_uv, 2, GL_FLOAT, GL_FALSE, 4 * sizeof(float), (void *)(2 * sizeof(float)));

    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    {
        GLenum e = glGetError();
        static GLenum last_e = 0xFFFF;
        if (e != last_e) { DBG("[draw] glError=0x%x win=%dx%d fbo_was=%d prog=%u\n", e, win_w, win_h, last_fbo, overlay_prog); last_e = e; }
    }

    glDisableVertexAttribArray(loc_pos);
    glDisableVertexAttribArray(loc_uv);

    // Restore full GL state
    if (last_fbo != 0) {
        glBindFramebuffer(GL_FRAMEBUFFER, last_fbo);
    }
    if (pfn_glBindVertexArray) {
        pfn_glBindVertexArray(last_vao);
    }
    glBindBuffer(GL_ARRAY_BUFFER, last_array_buffer);
    glUseProgram(last_program);
    glActiveTexture(last_active_texture);
    glBindTexture(GL_TEXTURE_2D, last_texture);

    if (last_blend) glEnable(GL_BLEND); else glDisable(GL_BLEND);
    glBlendFuncSeparate(last_blend_src_rgb, last_blend_dst_rgb, last_blend_src_alpha, last_blend_dst_alpha);

    if (last_depth_test) glEnable(GL_DEPTH_TEST); else glDisable(GL_DEPTH_TEST);
    if (last_cull_face) glEnable(GL_CULL_FACE); else glDisable(GL_CULL_FACE);
    if (last_scissor_test) glEnable(GL_SCISSOR_TEST); else glDisable(GL_SCISSOR_TEST);

    glColorMask(last_color_mask[0], last_color_mask[1], last_color_mask[2], last_color_mask[3]);
    glViewport(last_viewport[0], last_viewport[1], last_viewport[2], last_viewport[3]);
}

// =============================================================================
// Intercepted SDL3 Functions
// =============================================================================

bool SDL_GL_SetSwapInterval(int interval) {
    init_symbols();
    (void)interval;
    // Keep presentation unthrottled (interval 0) for lowest latency (~3.12 ms at 320 FPS)
    return real_SDL_GL_SetSwapInterval ? real_SDL_GL_SetSwapInterval(0) : false;
}

bool SDL_GL_SwapWindow(SDL_Window *window) {
    init_symbols();
    if (window) last_window = window;

    static bool swap_interval_configured = false;
    if (!swap_interval_configured) {
        if (real_SDL_GL_SetSwapInterval) {
            real_SDL_GL_SetSwapInterval(0);
            swap_interval_configured = true;
        }
    }

    static bool libroblox_unclamped = false;
    if (!libroblox_unclamped) {
        libroblox_unclamped = unclamp_process_framerate();
    }

    // Composite overlay directly into the current OpenGL backbuffer
    render_text_overlay(window);

    // Targeted Anti-AFK check (Zero focus stealing, affects only Mocktail)
    uint64_t now_ms = get_time_ms();
    bool do_pulse = false;

    if (g_afk_pulse_requested) {
        g_afk_pulse_requested = 0;
        do_pulse = true;
    }

    static uint64_t last_shm_poll = 0;
    if (!do_pulse && (now_ms - last_shm_poll >= 1500)) {
        last_shm_poll = now_ms;
        struct stat st;
        if (stat("/dev/shm/aeram_afk_pulse", &st) == 0) {
            if (st.st_mtime > last_shm_pulse_time) {
                last_shm_pulse_time = st.st_mtime;
                do_pulse = true;
            }
        }
    }

    // Autonomous pulse every 12 minutes (720s) if no input was registered
    if (!do_pulse && last_user_input_time > 0 && (now_ms - last_user_input_time >= 720000ULL)) {
        do_pulse = true;
    }

    if (do_pulse) {
        last_user_input_time = now_ms;
        if (!this_instance_typing) {
            inject_anti_afk_event(window);
        }
    }

    return real_SDL_GL_SwapWindow ? real_SDL_GL_SwapWindow(window) : false;
}

int SDL_PeepEvents(SDL_Event *events, int numevents, SDL_EventAction action, Uint32 minType, Uint32 maxType) {
    init_symbols();
    int count = real_SDL_PeepEvents ? real_SDL_PeepEvents(events, numevents, action, minType, maxType) : 0;
    if (events && count > 0 && (action == SDL_GETEVENT || action == SDL_PEEKEVENT)) {
        for (int i = 0; i < count; i++) {
            event_watcher(NULL, &events[i]);
        }
    }
    return count;
}

SDL_MouseButtonFlags SDL_GetMouseState(float *x, float *y) {
    init_symbols();
    return real_SDL_GetMouseState ? real_SDL_GetMouseState(x, y) : 0;
}

bool SDL_SetWindowRelativeMouseMode(SDL_Window *window, bool enabled) {
    init_symbols();
    if (window) last_window = window;
    uint64_t now = get_time_ms();

    if (enabled) {
        float mx = 0.0f, my = 0.0f;
        SDL_MouseButtonFlags flags = real_SDL_GetMouseState ? real_SDL_GetMouseState(&mx, &my) : 0;
        bool button_held = (flags & (SDL_BUTTON_RMASK | SDL_BUTTON_MMASK)) || button_drag_active;
        bool just_released = ((now - last_button_up_time) < 250);
        bool shift_active = ((now - last_shift_key_time) < 300);

        if (!button_held && just_released && !shift_active) {
            return true;
        }
        return real_SDL_SetWindowRelativeMouseMode ? real_SDL_SetWindowRelativeMouseMode(window, true) : false;
    } else {
        return real_SDL_SetWindowRelativeMouseMode ? real_SDL_SetWindowRelativeMouseMode(window, false) : false;
    }
}

void SDL_WarpMouseInWindow(SDL_Window *window, float x, float y) {
    init_symbols();
    if (window) last_window = window;
    if (!window && last_window) window = last_window;

    uint64_t now = get_time_ms();

    bool is_button_drag = button_drag_active || ((now - last_button_up_time) < 250);
    bool is_shift_event = ((now - last_shift_key_time) < 300);

    if (is_button_drag && !is_shift_event) {
        float target_x = (x > 0.0f || y > 0.0f) ? x : drag_start_x;
        float target_y = (x > 0.0f || y > 0.0f) ? y : drag_start_y;
        if (target_x <= 0.0f && target_y <= 0.0f) {
            target_x = drag_start_x;
            target_y = drag_start_y;
        }
        if (real_SDL_WarpMouseInWindow && window) {
            real_SDL_WarpMouseInWindow(window, target_x, target_y);
        }
        return;
    }

    int w = 0, h = 0;
    if (real_SDL_GetWindowSize && window) {
        real_SDL_GetWindowSize(window, &w, &h);
        if (w > 0 && h > 0 && real_SDL_WarpMouseInWindow) {
            real_SDL_WarpMouseInWindow(window, (float)w * 0.5f, (float)h * 0.5f);
            return;
        }
    }

    if (real_SDL_WarpMouseInWindow && window) {
        real_SDL_WarpMouseInWindow(window, x, y);
    }
}

bool SDL_ShowCursor(void) {
    init_symbols();
    return real_SDL_ShowCursor ? real_SDL_ShowCursor() : false;
}

bool SDL_HideCursor(void) {
    init_symbols();
    uint64_t now = get_time_ms();

    float mx = 0.0f, my = 0.0f;
    SDL_MouseButtonFlags flags = real_SDL_GetMouseState ? real_SDL_GetMouseState(&mx, &my) : 0;
    bool button_held = (flags & (SDL_BUTTON_RMASK | SDL_BUTTON_MMASK)) || button_drag_active;
    bool just_released = ((now - last_button_up_time) < 250);
    bool shift_active = ((now - last_shift_key_time) < 300);

    if (!button_held && just_released && !shift_active) {
        return true;
    }

    return real_SDL_HideCursor ? real_SDL_HideCursor() : false;
}

// =============================================================================
// Intercepted SDL3_ttf Functions (Fix trailing whitespace caret advancement)
// =============================================================================
static void *(*real_TTF_CreateText)(void *, void *, const char *, size_t) = NULL;
static bool (*real_TTF_SetTextWrapWidth)(void *, int) = NULL;
static bool (*real_TTF_SetTextWrapWhitespaceVisible)(void *, bool) = NULL;
static void **(*real_TTF_GetTextSubStringsForRange)(void *, int, int, int *) = NULL;

void *TTF_CreateText(void *engine, void *font, const char *text, size_t length) {
    if (!real_TTF_CreateText) {
        real_TTF_CreateText = (void *(*)(void *, void *, const char *, size_t))dlsym(RTLD_NEXT, "TTF_CreateText");
        if (!real_TTF_CreateText) real_TTF_CreateText = (void *(*)(void *, void *, const char *, size_t))dlsym(RTLD_DEFAULT, "TTF_CreateText");
    }
    if (!real_TTF_SetTextWrapWhitespaceVisible) {
        real_TTF_SetTextWrapWhitespaceVisible = (bool (*)(void *, bool))dlsym(RTLD_NEXT, "TTF_SetTextWrapWhitespaceVisible");
        if (!real_TTF_SetTextWrapWhitespaceVisible) real_TTF_SetTextWrapWhitespaceVisible = (bool (*)(void *, bool))dlsym(RTLD_DEFAULT, "TTF_SetTextWrapWhitespaceVisible");
    }
    void *t = real_TTF_CreateText ? real_TTF_CreateText(engine, font, text, length) : NULL;
    if (t && real_TTF_SetTextWrapWhitespaceVisible) {
        real_TTF_SetTextWrapWhitespaceVisible(t, true);
    }
    return t;
}

bool TTF_SetTextWrapWidth(void *text, int wrap_width) {
    if (!real_TTF_SetTextWrapWidth) {
        real_TTF_SetTextWrapWidth = (bool (*)(void *, int))dlsym(RTLD_NEXT, "TTF_SetTextWrapWidth");
        if (!real_TTF_SetTextWrapWidth) real_TTF_SetTextWrapWidth = (bool (*)(void *, int))dlsym(RTLD_DEFAULT, "TTF_SetTextWrapWidth");
    }
    if (!real_TTF_SetTextWrapWhitespaceVisible) {
        real_TTF_SetTextWrapWhitespaceVisible = (bool (*)(void *, bool))dlsym(RTLD_NEXT, "TTF_SetTextWrapWhitespaceVisible");
        if (!real_TTF_SetTextWrapWhitespaceVisible) real_TTF_SetTextWrapWhitespaceVisible = (bool (*)(void *, bool))dlsym(RTLD_DEFAULT, "TTF_SetTextWrapWhitespaceVisible");
    }
    bool ok = real_TTF_SetTextWrapWidth ? real_TTF_SetTextWrapWidth(text, wrap_width) : false;
    if (text && real_TTF_SetTextWrapWhitespaceVisible) {
        real_TTF_SetTextWrapWhitespaceVisible(text, true);
    }
    return ok;
}

void **TTF_GetTextSubStringsForRange(void *text, int offset, int length, int *count) {
    if (!real_TTF_GetTextSubStringsForRange) {
        real_TTF_GetTextSubStringsForRange = (void **(*)(void *, int, int, int *))dlsym(RTLD_NEXT, "TTF_GetTextSubStringsForRange");
        if (!real_TTF_GetTextSubStringsForRange) real_TTF_GetTextSubStringsForRange = (void **(*)(void *, int, int, int *))dlsym(RTLD_DEFAULT, "TTF_GetTextSubStringsForRange");
    }
    if (!real_TTF_SetTextWrapWhitespaceVisible) {
        real_TTF_SetTextWrapWhitespaceVisible = (bool (*)(void *, bool))dlsym(RTLD_NEXT, "TTF_SetTextWrapWhitespaceVisible");
        if (!real_TTF_SetTextWrapWhitespaceVisible) real_TTF_SetTextWrapWhitespaceVisible = (bool (*)(void *, bool))dlsym(RTLD_DEFAULT, "TTF_SetTextWrapWhitespaceVisible");
    }
    if (text && real_TTF_SetTextWrapWhitespaceVisible) {
        real_TTF_SetTextWrapWhitespaceVisible(text, true);
    }
    return real_TTF_GetTextSubStringsForRange ? real_TTF_GetTextSubStringsForRange(text, offset, length, count) : NULL;
}
