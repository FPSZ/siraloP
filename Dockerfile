# Competition image for the agent track (CTFY-0001 §4).
#
# Runtime is OFFLINE (internet_policy=deny) -> every tool is baked in here; the
# agent fetches NOTHING at run time. Built behind the GFW: a locally-present
# python base (no Docker Hub pull) + Tsinghua mirrors for apt / pip / rubygems.
#
# Toolchain curated for an AI agent driving a NON-INTERACTIVE persistent shell:
# scriptable / headless / deterministic-output tools over GUIs (see
# docs/testing/工具链.md for the research + rationale + the opt-in heavy tier).
#
# Layer order = cache strategy: slow-and-stable first (apt, radare2 source),
# volatile last (pip toolchain, then the agent package). Editing the agent never
# rebuilds a tool; tweaking the pip list never recompiles radare2.
FROM python:3.13-slim

# --- AI-agent shell ergonomics -------------------------------------------------
# The agent's shell has no TTY. Pagers (less/more) would BLOCK it forever; color
# escapes corrupt parsing. Kill both, unbuffer Python, quiet pwntools' terminal
# control. These env vars apply to every command the agent runs.
ENV DEBIAN_FRONTEND=noninteractive \
    PAGER=cat GIT_PAGER=cat MANPAGER=cat SYSTEMD_PAGER=cat \
    LESS=-F \
    NO_COLOR=1 CLICOLOR=0 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PWNLIB_NOTERM=1 PWNLIB_SILENT=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# --- Debian mirror (Tsinghua) so apt resolves behind the GFW at build time -----
RUN printf 'Types: deb\nURIs: https://mirrors.tuna.tsinghua.edu.cn/debian\nSuites: trixie trixie-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' \
      > /etc/apt/sources.list.d/debian.sources

# --- Tier: base + general (interpreters, build, file inspection, archives, net) -
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash build-essential gcc nasm git curl wget openssl ca-certificates \
      file binutils binutils-multiarch xxd bsdextrautils jq ripgrep \
      unzip 7zip zstd lz4 lzop cabextract squashfs-tools \
      netcat-openbsd socat \
    && rm -rf /var/lib/apt/lists/*

# --- Tier: rev + pwn (debuggers, tracing, ELF patching, multi-arch run) --------
RUN apt-get update && apt-get install -y --no-install-recommends \
      gdb gdb-multiarch strace ltrace patchelf checksec upx-ucl \
      qemu-user-static binfmt-support \
    && rm -rf /var/lib/apt/lists/*

# --- Tier: forensics / stego / misc (all CLI, no GUI) --------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
      libimage-exiftool-perl binwalk foremost scalpel \
      steghide stegseek outguess \
      zbar-tools pngcheck poppler-utils qpdf sleuthkit testdisk \
      sqlite3 fcrackzip pdfcrack \
    && rm -rf /var/lib/apt/lists/*

# --- Tier: crypto + web + net (local brute/solve, scanners, non-interactive) ---
RUN apt-get update && apt-get install -y --no-install-recommends \
      john gmp-ecm nmap sqlmap ffuf gobuster dirb \
      ruby ruby-dev \
    && rm -rf /var/lib/apt/lists/*

# --- radare2 (not in Debian trixie): build from source, REAL (copy) install ----
# The agent's go-to scriptable disassembler/analyzer (r2 -q -c '...', r2pipe).
# `ninja install` COPIES r2 into /usr/local (unlike sys/install.sh, which SYMLINKS
# the binaries/libs back into the build tree); that makes the ~0.5GB source+build
# tree pure residue, removed here. Installed footprint ~55MB vs ~540MB for a kept
# build tree. Libs land in /usr/local/lib/x86_64-linux-gnu, which is not on the
# default loader path, so register it before ldconfig. Placed EARLY (slow ~7 min
# compile, rarely changes) so the volatile pip/agent layers below reuse cache.
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config \
    && rm -rf /var/lib/apt/lists/* \
    && pip install meson ninja \
    && git clone --depth 1 https://github.com/radareorg/radare2 /tmp/r2src \
    && cd /tmp/r2src \
    && meson setup build --prefix=/usr/local --buildtype=release -Dlocal=false \
    && ninja -C build && ninja -C build install \
    && echo '/usr/local/lib/x86_64-linux-gnu' > /etc/ld.so.conf.d/radare2.conf \
    && ldconfig \
    && cd / && rm -rf /tmp/r2src

# --- Ruby gems (light, high-value; Tsinghua gem mirror) ------------------------
RUN gem sources --add https://mirrors.tuna.tsinghua.edu.cn/rubygems/ \
                --remove https://rubygems.org/ \
    && gem install --no-document one_gadget seccomp-tools zsteg

# --- Python toolchain (wheels via Tsinghua; the AI-friendly core) --------------
# pwntools (pwn+crypto interaction), gadget finders, capstone/unicorn/keystone,
# the crypto stack (pycryptodome/gmpy2/sympy/flint/fpylll/z3), web + forensics
# helpers, r2/rz scripting bridges. All import-as-library -> ideal for an LLM.
# cysignals is fpylll's runtime dep (lattice LLL/BKZ) and must precede it.
RUN pip install \
      pwntools ROPgadget ropper \
      capstone unicorn keystone-engine \
      pycryptodome gmpy2 sympy python-flint cysignals fpylll cryptography z3-solver \
      name-that-hash libnum xortool \
      requests "httpx[cli]" beautifulsoup4 lxml pyjwt \
      pillow numpy scapy \
      r2pipe rzpipe pyinstxtractor-ng uncompyle6

# --- gdb: non-interactive defaults (no pagination / no confirm prompts) --------
RUN printf 'set pagination off\nset confirm off\nset print pretty on\n' > /etc/gdbinit \
    && printf 'source /etc/gdbinit\n' > /root/.gdbinit

# A discoverable manifest so the agent can see what's installed (cat /opt/ctf-tools.txt).
RUN printf 'CTF toolchain (agent track). Drive non-interactively.\n\
pwn: pwntools, gdb/gdb-multiarch, ROPgadget, ropper, one_gadget, seccomp-tools,\n\
     patchelf, checksec, qemu-user-static, capstone/unicorn/keystone\n\
rev: radare2 (+r2pipe), binutils, strace/ltrace, upx-ucl, binwalk, uncompyle6,\n\
     pyinstxtractor-ng\n\
crypto: pwntools, pycryptodome, gmpy2, sympy, python-flint, fpylll, z3-solver,\n\
        cryptography, john, gmp-ecm, name-that-hash, xortool, libnum\n\
web: curl, jq, requests, httpx, nmap, sqlmap, ffuf, gobuster, dirb, pyjwt, bs4\n\
forensics: file, exiftool, binwalk, foremost, scalpel, steghide, stegseek,\n\
           outguess, zsteg, zbarimg, pngcheck, poppler-utils, qpdf, sleuthkit\n\
           (fls/mmls/icat), testdisk, sqlite3, fcrackzip, pdfcrack, scapy, pillow\n' \
      > /opt/ctf-tools.txt

# --- agent package (last, so editing agent/ doesn't rebuild the toolchain) -----
WORKDIR /app
COPY pyproject.toml /app/
COPY agent /app/agent
RUN pip install -e .

# IMPORTANT (CTFY-0001 §1.2): the platform sets cwd=/ctfy/work; invoke by module.
ENTRYPOINT ["python", "-m", "agent.entrypoints.image_main"]
