---
number: CTFY-0002
title: The ctfy challenge package
status: Active
version: 1
authors:
  - name: Yihang Wang
published: "2026-10-14"
revised: "2026-10-19"
updates: []
obsoletes: []
revisions:
  - version: 1
    date: "2026-10-14"
    breaking: false
    summary: >-
      The first version: the platform's behaviour as it stood, written
      down. No rule is new; each clause is read off the code that applies
      it.
    amendments:
      - date: "2026-10-14"
        change: >-
          §2.1: an upload's `name` is at most 100 characters and carries no
          control, format (text-direction, zero-width) or line-separator
          character (`authoring_name_too_long`, `authoring_name_unusable`).
          Every corpus name passes.
      - date: "2026-10-17"
        change: >-
          §3.5: the platform now holds an uploaded challenge's whole
          instance to a CPU and memory budget the operator sets (one core
          and 2 GiB by default), split over its services and applied over
          whatever the package declares, with 512 processes per service. A
          package's own limits are the weights of the split.
      - date: "2026-10-18"
        change: >-
          §3.5: the services' declared memory, summed, may not exceed that
          same budget (`authoring_memory_over_budget`); declared CPU over it
          is advice only (`authoring_cpu_over_budget`); a service declaring
          no limit is not counted and is advised about
          (`authoring_resources_undeclared`).
      - date: "2026-10-18"
        change: >-
          An optional `standard:` key in `metadata.yaml` (§2.8) says which
          version a package is written to. Absent, the package is checked
          against the current version; an unsupported version is refused
          (`standard_unsupported`, `META020`); the audit result records both
          the declared and the checked version.
      - date: "2026-10-19"
        change: >-
          §7.6: an optional `x-ctfy-sharing` key in `docker-compose.yml`
          says whether several teams may stand on one environment of the
          challenge (`shareable`, `per-connection`); absent is undeclared,
          and `COMPOSE011` checks its shape.
---

A **challenge package** is a directory: the files that make one
challenge, as its author uploads them. This standard says what the
platform does with one — which files it reads, which it refuses, how it
starts them, how it hands each team its answers, how it grades, how it
checks the package works, and who sees it afterwards. If you are writing
a challenge for this platform, this page is the contract.

**How to read it.** Two kinds of rule appear below, and they are
different on purpose:

- A **refusal** is applied when you upload. The upload is turned down,
  with a code and the reason, and nothing is stored. Every refusal code
  is in the appendix (Part B), with the clause it comes from.
- An **audit finding** is advice. The audit reads the package and reports
  findings with a rule code (`META005`, `STRUCT002`, …) and a severity,
  `error` or `warning`. On an upload the audit is evidence for the person
  reviewing it, never a gate. On the platform's own curated corpus an
  `error` fails the build. Every rule code is in the appendix (Part A),
  with the clause it comes from.

Check a package before you upload it, on your own machine, with no
platform and no token:

```bash
ctfy authoring check ./my-challenge
```

It runs the audit and the refusals an upload would meet that need no
competition, and it says which version of this standard it checked
against. `ctfy authoring check <competition> ./my-challenge`
additionally asks a platform for its own verdict, including the
competition's own rules.

`ctfy authoring init ./my-challenge --stack python-flask` writes a
package that already satisfies everything here.

---

## 1. Directory structure

### 1.1 The root

A package is any directory holding a `metadata.yaml`. Its id is the
directory's name; an `id:` written in `metadata.yaml` is ignored (and is
an audit finding, `META005`).

An uploaded package's id is never the name you chose. The platform mints
one, `AUTHORED-<8 hex>`, and your directory name is kept as the upload's
*slug* — the label you replace a pending upload by (§8).

### 1.2 Files the platform reads

| path | required | what it is |
|---|---|---|
| `metadata.yaml` | always | the challenge's description and its questions (§2) |
| `docker-compose.yml` | for `web` and `pwn` (`STRUCT001`) | the services that run per team (§3) |
| `services/<name>/` | when a compose service has `build:` (`STRUCT003`); each build context needs a `Dockerfile` (`STRUCT004`) | the source of each service |
| `solution/exploit.py` **or** `solution/Dockerfile` | unless the item is question-and-answer only (`STRUCT002`) | the reference solution (§6) |
| `attachments/` | for `pwn`, `reverse`, `crypto`, `misc` unless question-and-answer only: at least one file at its top level (`STRUCT010`) | files every team downloads; `*.tpl` files are rendered per team (§4.3) |
| `audit-allowlist.yaml` | no | `allowlist: [{rule, path, line?, reason}]` turns a matching audit `error` into a `warning` |
| `params/` | no — written at run time by a seeder (§4.5) | never shipped |

The platform reads no other file by name. There is no ignore file: the
CLI's packer leaves out `.git`, `__pycache__`, `node_modules`, `.venv`
and `.DS_Store`, and lists anything else it leaves out (§7.3).

### 1.3 Reserved names

Three compose service names are a contract with the platform, and a
service by one of these names is treated as that sidecar (§4):

| service | role |
|---|---|
| `flag-rotator` | writes each team's minted answers into the box (§4.2) |
| `attachment-builder` | builds a per-team file into `attachments/` (§4.4) |
| `seeder` | plants a value through the running app and publishes `params/` (§4.5) |

### 1.4 What a category requires

`category:` (§2.1) decides what the audit expects to find:

| category | needs `docker-compose.yml` | needs `attachments/` | may be question-and-answer only |
|---|---|---|---|
| `web` | yes | no | no |
| `pwn` | yes | yes | no |
| `reverse` | no | yes | yes |
| `crypto` | no | yes | yes |
| `misc` | no | yes | yes |

An item is **question-and-answer only** when its category is `misc`, it
has no `docker-compose.yml`, and every question carries a static
`answer:`. Such an item cannot be uploaded (§2.7); it exists in the
curated corpus only.

### 1.5 What an upload refuses about the archive

An upload is a `.tar.gz` (a plain `.tar` is accepted too), and it is
attacker-controlled input, so it is read member by member before
anything is written. **One unsafe member refuses the whole archive.**

- A single common top-level directory is stripped; after that,
  `metadata.yaml` must be at the root (`archive_no_metadata`). An archive
  with no members is `archive_empty`.
- Every member must be a regular file. A symlink, a hard link, a device
  or a fifo is `archive_member_not_a_file`.
- Every path must stay inside the package: not empty (`path_empty`), not
  absolute (`path_absolute`), not starting with `~`
  (`path_home_directory`), no `..` segment (`path_escapes`), no empty or
  `.` segment (`path_dot_segment`), no backslash or NUL
  (`path_backslash_or_nul`).
- Under `attachments/`: no hidden name (`attachment_hidden`), no deeper
  than 16 levels (`attachment_too_deep`), no control character, bidi
  override or backslash in a name (`attachment_name_unusable`), at most
  2 000 files (`attachments_too_many`).
- The size limits are in §7.4.

---

## 2. `metadata.yaml`

### 2.1 Top-level keys

The file is a YAML mapping. A file that does not parse, or is not a
mapping, is `META000`, and the audit then reports nothing else.

| key | type | required | default | checked by |
|---|---|---|---|---|
| `name` | string | yes | — | `META002` |
| `description` | string | yes | — | `META002`, `META007`, `META010`, `META014` |
| `difficulty` | `easy` \| `medium` \| `hard` | yes | — | `META002`, `META003` |
| `tags` | non-empty list of strings | yes | — | `META002`, `META004` |
| `questions` | non-empty list (§2.2) | no | one dynamic question, id `flag`, prompt "Capture the secret" | `META006`, `META008`, `META009`, `META010`, `META014`, `META015`, `META018` |
| `category` | `web` \| `pwn` \| `reverse` \| `crypto` \| `misc` | no | `web` | `META011` |
| `network_topology` | `simple` \| `engagement` | no | `simple` | `META012` (§3.6) |
| `vpn_backend` | `openvpn` \| `wireguard`, only with `engagement` | no | `openvpn` | `META016` |
| `entry_urls` | list of absolute `http`, `https` or `tcp` URLs (`tcp` needs a port) | no | `[]` | `META013`; with `engagement` also `META017`, `META019` |
| `edge_service` | string: the service a player connects to | no | inferred (§3.6) | — |
| `awd` | mapping: an attack & defence gamebox | no | — | `AWD001`–`AWD010` (§3.9) |
| `patch` | mapping: the patchable surface | no | — | `PATCH001`–`PATCH006` (§3.9) |
| `standard` | `CTFY-0002/<version>` | no | the current version | `META020` (§2.8) |

Any other top-level key is `META005`. The retired keys of an older
schema — `flags`, `vars`, `public_vars`, `flag_path`, `flag_inject`,
`id`, `origin`, `provider`, `provider_config`, `hint_levels` — get a
hint naming their replacement.

⚠️ The platform's own reader ignores unknown keys rather than refusing
them, so a misspelled key is caught by the audit and by nothing else.

An upload's `name` is shown on every surface that lists a challenge (the
catalog, the scoreboard, mail, the CLI), so the upload door asks two
things of it:

- at most 100 characters (`authoring_name_too_long`);
- no character in Unicode categories `Cc`, `Cf`, `Zl` or `Zp`: no newline,
  tab, zero-width or text-direction character
  (`authoring_name_unusable`, which names each one by its code point).

A corpus challenge never meets the door, and every corpus name would pass
it.

The `description` is shown to every team before they start, so:

- it states the goal and never the vulnerability class, the technique or
  an internal endpoint (`META007`, a warning — it matches words, and a
  word list cannot be a gate);
- it never paraphrases a gated question's prompt (`META010`);
- it never reaches for an answer (`META014`, §2.6).

### 2.2 Questions

Each entry of `questions:` is a mapping. Its keys:

| key | meaning | default |
|---|---|---|
| `id` | the question's id (§2.4) | required |
| `prompt` | what the team is asked; non-empty | required |
| `answer` | a fixed answer: a string, or a list for a multi-select | absent |
| `choices` | the options of a select question | absent |
| `requires` | ids of questions that must be solved first (§2.5) | `[]` |
| `format` | `flag` or `raw` | `flag` |
| `match` | how a submission is compared (§5.1) | `{mode: exact, case_sensitive: false}` |
| `check` | a sidecar that decides (§5.4): `service`, `cmd`, `timeout_s` (30), `poll` (false), `poll_interval_s` (60) | absent |
| `answer_from` | `seeder`: the challenge supplies the answer (§4.5) | absent |
| `scores` | `capability` or `reporting` — which axis of the board it pays | `capability` |

Any other key, a missing `id` or `prompt`, a wrong type or a malformed
`check:` is `META006`. An unknown `scores:` is `META018` — never a quiet
fall-back to `capability`.

⚠️ A malformed question is dropped by the platform's reader, not
refused, so the audit is where you learn of it.

### 2.3 How a question's mode is inferred

A question never declares its mode. It is inferred from which keys it
carries, in this order:

1. `check:` with no `answer` and no `choices` → **checker**: a sidecar
   decides (§5.4).
2. `answer_from: seeder` with no `answer` and no `choices` → **seeded**:
   the challenge supplies the answer per instance (§4.5).
3. no `answer` and no `choices` → **dynamic**: the platform mints a fresh
   answer for every team (§4.1).
4. a string `answer`, no `choices` → **static**.
5. a string `answer` with `choices` → **single-select**.
6. a list `answer` with `choices` → **multi-select**.

Anything else — `choices` without an `answer`, `check:` beside an
answer, an `answer_from` other than `seeder` — has no mode and is
`META008` (or `META006` for `check:` beside an answer).

### 2.4 Question ids

An id matches `^[a-z][a-z0-9_]*$` and is unique within the package
(`META006`). It becomes a file name (`/answers/<id>`) and an environment
variable (`ANSWER_<ID>`, the id upper-cased), which is why the alphabet
is that narrow.

### 2.5 `requires:`

Each id in `requires:` must name another question of the same package
(`META009`), never the question itself, and the graph must have no
cycle. A question whose `requires:` is non-empty is **gated**: it is not
shown until those are solved, and the description may not paraphrase it
(`META010`).

### 2.6 Prompts and per-instance values

The description and each prompt may use Jinja placeholders of exactly
one name, `param`: `{{ param.port }}`. The values come from
`params/prompt.json`, which a seeder writes per instance (§4.5). A text
with no `{{` or `{%` is shown unchanged.

Nothing else is bound. A text that reaches for `answer`, or for any name
other than `param` in a text that also uses `param.`, is `META014` — a
prompt is published to the team, and an answer in it would be published
too.

### 2.7 What an upload requires of the questions

An upload is refused unless:

- it has at least one question the platform can read
  (`authoring_requires_a_question`);
- every question is answered per instance — **dynamic**, **checker** or
  **seeded** (`authoring_requires_minted_answer`). A static or select
  answer would be the same for every team, so it cannot be uploaded;
- something in the package delivers each answer
  (`authoring_answer_undelivered`): a dynamic answer through a running
  service or a per-team file (`attachments/*.tpl` or an
  `attachment-builder`); a checker or seeded answer through a running
  service. A service under a compose `profiles:` does not count as
  running;
- in a competition with no end date, at least one answer is minted per
  instance (`standing_needs_minted_answer`).

### 2.8 Declaring the standard

A package may say which version of this standard it is written to, in
one line:

```yaml
standard: CTFY-0002/1
```

- **Absent:** the package is checked against the current version, and
  the audit result says *not declared, checked against v1*. Nothing has
  to be added to a package written before this key existed.
- **A version the platform supports:** the package is checked against
  that version. If a newer one exists, the audit result lists the
  versions it has not caught up with and what each changed — advice,
  never a refusal.
- **Anything else** — a version this platform does not know, or a value
  not of the form `CTFY-0002/<n>`: an upload is refused
  (`standard_unsupported`), naming the versions the platform supports;
  the audit reports it as `META020`.

The audit result records the declared value and the version actually
checked against, on every surface that shows an audit: the author's
page, the review queue and `ctfy authoring check`. `ctfy authoring init`
and every template write the current version.

---

## 3. Compose

### 3.1 Which file, and when it is ruled on

Only `docker-compose.yml` at the package root is read, and only it is
run. An upload rules on it before anything is stored, and **every**
refusal is collected and returned together. A file that does not parse
is `compose_unparseable`; one that is not a mapping is
`compose_not_a_mapping`. A package with no compose file is not refused
here.

⚠️ The rule is an **allow-list that fails closed**: a key the platform
does not know is refused, not passed through. It buys the declarative
surface only — a `Dockerfile`'s `RUN` still runs on a worker when the
image is built (§7.1, §7.5).

### 3.2 Top-level keys

Allowed: `services`, `volumes`, `networks`, `name`, and any key starting
with `x-`. Anything else is `compose_top_level_key` — `include:`,
`configs:`, `secrets:` and `version:` among them (`version:` is also the
audit's `COMPOSE002`). A network declared `external: true` is
`compose_external_network`.

`x-ctfy-internet` (§7.1) and `x-ctfy-sharing` (§7.6) are the two extensions
the platform reads. Their shapes are `COMPOSE010` and `COMPOSE011`.

### 3.3 Service keys

**Allowed:** `build`, `cap_add`, `command`, `depends_on`, `deploy`,
`dns`, `entrypoint`, `environment`, `expose`, `healthcheck`, `hostname`,
`image`, `labels`, `links`, `network_mode`, `networks`, `ports`,
`profiles`, `restart`, `shm_size`, `stop_grace_period`, `stop_signal`,
`sysctls`, `tty`, `ulimits`, `user`, `volumes`, `working_dir`.

**Refused with their own code:**

| key | code | why |
|---|---|---|
| `container_name` | `compose_container_name` | the platform names every container itself |
| `extra_hosts` | `compose_extra_hosts` | `host-gateway` is a route to the worker |

**Every other service key** is `compose_service_key` — including
`privileged`, `pid`, `ipc`, `devices`, `security_opt`, `userns_mode`,
`cgroup_parent`, `runtime`, `cap_drop`, `env_file`, `mem_limit`,
`cpus`, `read_only`, `tmpfs`, `init`, `platform` and a service-level
`x-*`.

### 3.4 Values the upload checks

| key | allowed | code |
|---|---|---|
| `cap_add` | `NET_ADMIN`, `IPC_LOCK`, `NET_RAW`, `NET_BIND_SERVICE` | `compose_capability` |
| `sysctls` | `net.ipv4.ip_forward` | `compose_sysctl` |
| `labels` | anything not starting `traefik.` or `com.docker.` (`ctfy.` is allowed) | `compose_label` |
| `network_mode` | `none`, or `service:<name>` | `compose_network_mode` |
| `volumes` (short form) | a named volume; a bind only to a path inside the package | `compose_bind_outside` |
| `volumes` (long form) | `type:` absent, `volume`, `tmpfs`, or `bind` inside the package | `compose_volume_type`, `compose_bind_outside` |
| `build` (string) | a path inside the package | `compose_build_outside` |
| `build` (mapping) keys | `context`, `args`, `dockerfile`, `additional_contexts`, `network` | `compose_build_key` |
| `build.context`, `build.dockerfile` | a path inside the package | `compose_build_path_outside` |
| `build.network` | `none` or `default` | `compose_build_network` |

A path leaves the package when it is empty, starts with `/` or `~`,
contains a backslash, or has a `..` segment.

The other allowed keys' values are not inspected by the upload. `ports:`
is allowed and rewritten when the box starts (§3.6).

### 3.5 Resources and health

The upload does not require resource limits; the audit expects them
(`COMPOSE006`): every service sets `deploy.resources.limits.cpus` and
`.memory`.

The platform **applies** a budget to every instance of an uploaded
package, all its services together: by default one CPU and 2 GiB of
memory, set by the operator. It is split over the services the stack
starts — each weighs what it declares, or one CPU and 512 MiB when it
declares nothing, and when the weights exceed the budget every service is
scaled down in proportion — and written over the package's own values,
with at most 512 processes per service. A one-shot `profiles:` service
(the `attachment-builder`) runs alone and is held to the whole budget. So
a package that needs its budget concentrated in one service declares
limits that say so. A worker counts the whole budget when it places the
box.

The upload asks the same question first. The memory
(`deploy.resources.limits.memory`) the services of the stack declare,
summed, may not exceed the budget the platform would apply
(`authoring_memory_over_budget`): a package asking for more would be
squeezed at run time and may crash, so it is refused instead. Declared CPU
(`deploy.resources.limits.cpus`) over the budget is only advised about
(`authoring_cpu_over_budget`): CPU is compressible and the runtime caps every
instance to the budget anyway, so it slows the box rather than breaking it. A service declaring nothing is not
counted, and the check advises about it (`authoring_resources_undeclared`);
`ctfy authoring check <dir>` with no competition compares against the
platform's default budget. Published packages are not re-judged.

Every service needs a `healthcheck` (`HEALTH001`), except a one-shot that
others wait on with `condition: service_completed_successfully` and the
`attachment-builder`. `docker compose up --wait` is how the platform
knows a box is ready, so a service with no health check is ready the
moment it starts.

### 3.6 Network topology

`network_topology:` (§2.1) chooses one of two shapes.

**`simple`** (the default). Every `ports:` entry is removed when the box
starts; each service that declared one is the team's entry. It gets a
random host name on the platform's ingress and is joined to the ingress
network beside its own `default`. HTTP is routed by host name; a service
labelled `ctfy.service.protocol: tcp` is routed as TLS by SNI. The audit
reads that label (`STRUCT012`, `STRUCT013`, `STRUCT014`, `STRUCT022`); at
least one service must publish a port (`COMPOSE003`). How a team reaches a
`tcp` service — TLS by SNI on port 443, a port of its own, or the VPN — is the
competition's choice, not the package's.

**`engagement`.** The team reaches the box through a VPN rather than the
ingress. Unless the package declares its own VPN service, the platform
adds one, plus a resolver (`edge-dns`), on a `dmz` network it adds too,
and joins the **edge** service to it. The edge is `edge_service:` if
given, else the only long-running service, else the only one that
publishes a port; otherwise the start fails. Every other `ports:` entry
is removed. `entry_urls` hosts must be names the team's own machine can
resolve through the tunnel (`META017`, `META019`). A package that
declares its own VPN service is held to `NET001`–`NET005`.

### 3.7 How the box is started

Per team, the platform runs `docker compose build` and then
`docker compose up -d --wait` over `docker-compose.yml` plus an override
of its own. An uploaded package is always built from source — never
from a pre-built image of it. A build that fails fails the start; so
does a service that never becomes healthy.

### 3.8 What else the audit reads in compose

`COMPOSE001` (`services:` is a mapping), `COMPOSE004` (no Dockerfile but
the rotator's bakes an answer), `COMPOSE005` (no `ANSWER_` build arg),
`COMPOSE007` (no service hard-codes its own public URL — the platform
injects it as `CTFY_SERVICE_URL_<SERVICE>` and `CTFY_INSTANCE_URL`),
`COMPOSE008` (every `COPY`/`ADD` source exists), `COMPOSE009` (a `RUN`
that starts a database also stops it).

### 3.9 Attack & defence gameboxes

An `awd:` block makes the package a gamebox for an attack & defence
match, and `patch:` declares what a defender may change. Their audit
rules are `AWD001`–`AWD010` and `PATCH001`–`PATCH006`. The checker
contract between the platform and a gamebox (`put` / `get` / `check`)
is not part of this version; it will be its own standard.

---

## 4. Answer delivery

### 4.1 The channels, and when each runs

A dynamic answer is minted by the platform for every team, and it never
leaves the platform except into that team's box. There are four ways a
package receives one, and they run at different moments of a start:

| order | step | channel |
|---|---|---|
| 1 | the package is copied for this team | — |
| 2 | `attachments/*.tpl` are rendered | templates (§4.3) |
| 3 | `attachment-builder` runs, before the services | builder (§4.4) |
| 4 | `docker compose up -d --wait` | rotator (§4.2), seeder (§4.5) |
| 5 | `params/prompt.json` and `params/answer.json` are read | seeder (§4.5) |

Every minted answer is in the compose environment as `ANSWER_<ID>`; a
service receives it only by naming it in its own `environment:`.

### 4.2 `flag-rotator`

The rotator is a one-shot service that names each `ANSWER_<ID>` it owns
in `environment:` and writes each value, verbatim, to a shared volume —
the scaffold writes `/out/<id>`. The service that needs the answer mounts
that file at `/answers/<id>` and waits on
`flag-rotator: condition: service_completed_successfully`. Write the file
in place: a single-file mount binds an inode, so a rename leaves the app
reading the old one.

The audit: the rotator is required for every dynamic answer no checker,
builder or seeder delivers, and it names exactly those (`STRUCT006`);
some service consumes it (`STRUCT007`); it publishes no port
(`STRUCT008`); no service mounts `/answers/<id>` for a static or unknown
id (`STRUCT009`).

### 4.3 Templates: `attachments/*.tpl`

A file under `attachments/` ending in `.tpl` is rendered per team, as
Jinja, before anything starts. Two names are bound: `answer.<id>` (the
minted values) and `instance.id`. The result drops the `.tpl` suffix and
the template itself is not served. A template that does not render fails
the start.

The audit: a template names only dynamic ids that exist (`STRUCT017`)
and parses (`STRUCT018`); a package with no compose file delivers every
dynamic answer through some template (`STRUCT011`).

### 4.4 `attachment-builder`

For a per-team file a template cannot produce — a binary built around
the answer. The service carries `profiles: ["ctfy-builder"]`
(`STRUCT019`), names the `ANSWER_<ID>` it needs, bind-mounts
`./attachments` to `/out` (`STRUCT021`) and publishes no port
(`STRUCT020`). The platform runs it once, before the services, as
`docker compose --profile ctfy-builder run --rm --no-deps --build
attachment-builder`, with 300 seconds to finish. A non-zero exit or a
timeout fails the start.

### 4.5 `seeder` and `params/`

A seeder is an ordinary compose service that runs after the app is
healthy and talks to it through the app's own interface. It has two
uses:

- **plant a minted answer**: it names `ANSWER_<ID>` in its own
  `environment:`, and that id is then off the rotator's list
  (`STRUCT006`);
- **supply a per-instance answer**, for a question with `answer_from:
  seeder` (§2.3). It writes, through a `./params:/out` bind mount:
  - `params/answer.json` — an object keyed by **question id**; each value
    is the answer (a string, or for `contains` the answer set of §5.3);
  - `params/prompt.json` — an object of values the prompts render as
    `{{ param.<name> }}` (§2.6);
  - `params/manifest.json` — for the package's own judge; the platform
    never reads it.

A `params/` file that is missing, is not a JSON object, or is a symlink
is read as empty: the instance still starts, and a seeded question on it
cannot be answered. The audit requires the seeder's Python to parse
(`SOURCE005`).

### 4.6 What may never carry an answer

No Dockerfile but the rotator's bakes one (`COMPOSE004`), no build
argument is named `ANSWER_*` (`COMPOSE005`), and no source file under
`services/` carries a literal `FLAG{…}` (`SOURCE001`).

---

## 5. Grading

### 5.1 `match:`

| field | values | default |
|---|---|---|
| `mode` | `exact`, `contains`, `judge` | `exact` |
| `case_sensitive` | boolean | `false` |

An unknown mode is `META015`. `case_sensitive` changes `contains` only;
an exact comparison is always case-sensitive.

### 5.2 `exact`

By mode:

- **dynamic** and **seeded**: both sides stripped of whitespace, then
  compared as strings; a match with the `FLAG{…}` wrapper removed from
  either side also counts.
- **static**: both sides stripped, compared as strings.
- **single-select**: the value must be one of `choices` and equal the
  answer.
- **multi-select**: a list, or a string split on commas and whitespace;
  every part must be one of `choices`, and the set must equal the
  answer's.

A list sent to a question that takes one value is refused — not graded
— and costs no attempt.

### 5.3 `contains`

Only on a **seeded** question (`META015`). The seeder's value for it is
an answer set, `{"any": [...], "none": [...], "at_least": N}` (a bare
string is a set of one). A submission is accepted when at least `N`
(default: all) of `any` appear in it as substrings and none of `none`
appear as a word. An empty set, an empty submission or an `at_least`
outside `1..len(any)` is wrong.

### 5.4 Checker and `judge`

A **checker** question has no answer for the team to submit: the
package's own sidecar (`check.service`, running `check.cmd`) prints this
question's minted token when the team has achieved the effect it
checks. With `match: {mode: judge}` the team submits a value and the
sidecar is given it as one more argument; the answer is accepted only if
the sidecar prints this question's own token. A submission longer than
4 096 characters is not passed. The judge needs `check:` (`META015`),
must exist in compose and name the question's `ANSWER_<ID>`
(`STRUCT015`), and publishes no port (`STRUCT016`).

When the platform cannot run the judge — the worker is gone or silent,
out of time — the submission is neither right nor wrong, and costs no
attempt.

### 5.5 Wrong-attempt caps

Questions whose answer space is bounded are capped, counted in wrong
answers since the last reset by an organiser, and checked **before**
grading, so the cap cannot be used as an oracle. The defaults:
single-select 3, multi-select 10, static 10, seeded 10, judge 10. A
dynamic or checker question is never capped. An operator may change the
caps.

### 5.6 The author's own team

Your own team is graded on your own upload — pressing submit is the only
way to check from outside that it is answerable — and scores nothing for
it: no solve is recorded, so it takes no first blood and decays no
points.

---

## 6. Reference solution and smoke

### 6.1 The solution

Every package that runs ships one, as `solution/exploit.py` or
`solution/Dockerfile` (`STRUCT002`). A Dockerfile is built from the
package root with `docker build -f solution/Dockerfile` and run with its
own entrypoint; the language is yours. Otherwise `solution/exploit.py`
runs in the platform's `ctfy-exploit-runner` image, which carries Python,
`requests` and the `ctfy` package, so it needs nothing installed.

An `exploit.py` imports `ExploitBase` and `run_exploit` from
`ctfy.challenge.exploit` (`EXPLOIT002`), defines `class Exploit
(ExploitBase)` with a `run` method (`EXPLOIT003`), and calls
`run_exploit` under `if __name__ == "__main__":` (`EXPLOIT004`). It must
parse (`EXPLOIT001`), and it must rediscover each answer the way a team
would — it may not read `ANSWER_*`, `/answers/`, `params/answer.json` or
`params/manifest.json` in its own process (`EXPLOIT007`).
`params/prompt.json` is the team's own view and is available as
`self.params`.

A solution in another language is a `solution/Dockerfile`. It is built
like a service (§7.5) and held to the same contract (§6.2); the audit's
`EXPLOIT` rules read `exploit.py` only.

```dockerfile
# solution/Dockerfile — the default runner, said explicitly
FROM ctfy-exploit-runner
ENTRYPOINT ["python3", "solution/exploit.py"]
```

### 6.2 The contract

- **Input.** The target as two arguments, `<host> <port>`, and as
  `CTFY_TARGET_HOST` / `CTFY_TARGET_PORT`; each sibling service as
  `CTFY_SERVICE_URL_<SERVICE>`.
- **Output.** One line `Q[<question_id>]: <value>` on stdout per answer
  found. `run_exploit` prints these.
- **Exit code.** `0` captured; `1` ran and failed; `2` crashed; `3`
  captured nothing; `4` could not reach the target.

What the solution must print: every dynamic answer except a checker's,
and every seeded one. A dynamic value must equal the minted one, after
stripping whitespace; a seeded one is only checked for being present.

### 6.3 Where it runs

Non-root (`65534:65534`), on a read-only root filesystem with a writable
`/tmp` (512 MiB), with every capability dropped and `no-new-privileges`,
1 024 processes, 2 GiB of memory and 2 CPUs. The package is copied into
`/chal`, which is the working directory. It is attached to the box's own
compose networks — or, for an `engagement` package, to a tunnel the way a
team connects — and to nothing else: no internet, no docker socket, no
host directory.

### 6.4 The smoke

A **smoke** is a real start of the package on a worker, followed by its
solution, followed by teardown — the way a team would meet it:

1. the package is placed on the worker;
2. answers are minted; the box is started as in §3.7 and §4.1 —
   templates, builder, build, `up --wait`, health;
3. for `engagement`, the tunnel is brought up as a team's would be;
4. the solution is built (if it is a Dockerfile) and run;
5. what it printed is compared with what was minted (§6.2);
6. the box is torn down and its images removed.

| verdict | meaning |
|---|---|
| `solved` | every answer was found and right |
| `unsolved` | the box ran and the solution missed or got an answer wrong |
| `estate_failed` | one of your own services failed to build or become healthy, or the solution image failed to build |
| `malformed` | the package could not be read |
| `errored` | the platform could not finish the run — never your fault, and never charged to you |

### 6.5 What the log shows you

Each run's log lists its steps — `build`, `up`, `health`, `reach`,
`exploit`, `grade` — each `ok`, `failed`, `timed_out` or `skipped`, with
the exit code, the tail of the output, and for `grade` what was printed
against what was expected. Only your own services' output and your own
solution's are recorded, and the worker's names, paths and secrets are
replaced by `<node>`, `<tree>`, `<tmp>`. A step keeps 8 KiB, a log
64 KiB, and the last 10 logs of an upload are kept.
`ctfy authoring smoke-log <competition> <id>` prints them.

### 6.6 When a smoke runs

On upload, within a per-person allowance over a rolling 24 hours that the
operator sets (`0` turns it off); a competition with automatic review
uses its team quota instead. A smoke on upload is evidence for the
reviewer, never a gate on publishing. A published upload is smoked again
from time to time, and a failure is mailed to its reviewers once per
change from passing to failing.

### 6.7 Smoking on your own machine

`ctfy authoring smoke --local ./my-challenge` runs the same solution in
the same image against your box. It exits `0` solved, `1` wrong or
nothing captured, `2` the box or the solution image failed, `3` the
package is missing or malformed.

---

## 7. Runtime environment

### 7.1 The network

**While a box runs**, whether it reaches the internet is the
competition's policy: allowed, denied, or decided per challenge. Under
the per-challenge policy the package states what it needs:

```yaml
x-ctfy-internet:
  required: true
  reason: The app fetches exchange rates at start.
```

(`COMPOSE010`: a mapping with only `required` and `reason`; a reason is
required when `required` is true, at most 500 characters.) A denied box
has no route out and no outside DNS. The smoke always runs denied.

**While an image builds**, on a worker that runs uploaded code, a build
cannot reach another network's published port. `build.network` may be
`none` or `default`, never `host` (§3.4).

The reference solution never reaches the internet (§6.3).

### 7.2 Executable bits

The owner-execute bit of each file travels with the upload and is
restored when the package is written on a worker: an executable is
`0755`, anything else `0644`. No other mode bit, owner or timestamp
survives. An archive that records no execute bit at all is read as
*not recorded*: a file that starts with `#!` or is an ELF binary is then
made executable.

### 7.3 Packing

The CLI (`ctfy authoring upload` and `check`) packs a directory itself:
it leaves out `.git`, `__pycache__`, `node_modules`, `.venv` and
`.DS_Store`, refuses to pack symlinks and special files, and names each
thing it left out.

### 7.4 Size limits

| limit | value |
|---|---|
| the archive as uploaded | the operator's setting, 64 MiB by default |
| entries in the archive | 2 000 (`archive_too_many_entries`) |
| one file | 64 MiB (`archive_member_too_large`) |
| all files together | 128 MiB (`archive_unpacks_too_large`) |
| files under `attachments/` | 2 000, at most 16 levels deep |

The platform measures the unpacked size while it reads, so an archive
that expands past the limit is stopped without being unpacked. A path
has no length limit of its own.

### 7.5 Build time, and the review of what a package runs

A service is built from its own `Dockerfile` on a worker reserved for
uploaded code. Any `FROM` image is allowed. The worker holds no
credential to the platform's own registry.

What a package runs is reviewed by a person before it is published: the
compose file, every Dockerfile, everything under `solution/` and inside
a build context, and any executable or archive under `attachments/`. A
package with any of these needs that review once, and a replaced package
arrives unreviewed.

---

### 7.6 Sharing one environment

An organiser may let several teams stand on one running environment of a
challenge instead of one each. The package says whether that is safe; the
platform never guesses:

```yaml
x-ctfy-sharing: shareable        # or: per-connection
```

`shareable` — several teams may use one environment (read-only, stateless,
or otherwise unharmed by a neighbour). `per-connection` — the challenge
isolates every connection itself (a forking or `nsjail` entry service), so
sharing the environment shares nothing a player can touch. Absent is
*undeclared*: sharing it is the organiser's call and they are warned, never
blocked. (`COMPOSE011`: one of the two words.) A challenge with the
`engagement` topology cannot be shared at all.

A shared environment is started once for its group of teams and its minted
answers are generated once, so every team of the group submits the same
value.

## 8. Versions and revisions

### 8.1 Every upload is a version

Every tree you store — the first upload, each re-upload while it waits,
each proposed revision — is kept as a version, numbered v1, v2, … for the
challenge, with its files and each file's execute bit, your change note,
its smoke run and its review verdict. A tree identical to the current one,
execute bits included, stores no new version. Any two versions can be
compared, and each downloaded. A version's submission time is what a
player's report, a re-check's failure and every "since this version" rule
count from.

One version waits for review at a time: storing a new one while another
waits withdraws the waiting one, which stays in the history. Only a
version waiting for review can be withdrawn by its author
(`version_not_withdrawable`); a version the platform recorded without its
files cannot be compared or downloaded (`version_tree_not_kept`).

### 8.2 Replacing a pending upload

Upload again under the same slug (the directory's name) while the first
is still pending, and it is replaced: the same id, a new version, the
previous one withdrawn and kept (§8.1) — and not charged against your
pending cap. An upload that has been approved cannot be replaced
(`authored_challenge_already_reviewed`): revise it (§8.3). One that was
rejected stays on record; upload the new attempt under a new name.

### 8.3 Revising an approved upload

Any of its recorded authors may propose a **revision**: a whole new
tree for the same id. A revision passes every rule an upload passes. It
is reviewed as a diff against the live tree, by a reviewer of the
competition, and applied by a platform administrator.

- **Question ids may only grow.** A revision that drops a question the
  platform serves today is refused (`revision_drops_question`); a missing
  `questions:` counts as the default `flag` question.
- A revision identical to the live tree, execute bits included, is
  refused.
- One proposal per upload: a new one replaces the one waiting.
- A revision changes the files only. The declarations, the contributor
  agreement and the destination stay the upload's.
- Applying swaps the published directory whole, so a box already running
  keeps the tree it started with. The version and the smoke verdict move
  with the new tree; the smoke verdict starts empty.
- In a competition with no end date, the new tree must still mint an
  answer per instance (`revision_curated_into_standing`).

### 8.4 Which version players get

One version is live: a new box is built from it, and a box already running
keeps the tree it started with. Only a platform administrator changes which
one — never an author — by applying an approved proposal or rolling back to
an earlier approved version, now or once every running competition that
includes the challenge has ended. Every switch is recorded, with who made it
and why.

- Only an approved version can be made live (`switch_not_approved`,
  `switch_upload_not_approved`), and not the one already live
  (`switch_already_live`).
- A rollback needs a reason (`switch_needs_reason`), and question ids only
  grow, so a version missing a question the live one serves is refused
  (`switch_drops_question`).
- A new tree's execution surface is confirmed again
  (`switch_exec_surface_unreviewed`), and in a competition with no end date
  it must mint an answer per instance (`switch_curated_into_standing`).
- A switch cannot wait for a competition with no end date
  (`switch_never_ends`); one switch waits at a time
  (`switch_already_scheduled`); and a switch whose live version changed
  while it applied is refused (`switch_changed`). A waiting switch that can
  no longer apply when its moment comes is skipped, and its administrator
  is told.
- A competition may lock its challenges' versions while it is in play
  (`lock_challenge_versions`): a switch or rollback is then refused
  (`version_switch_locked`, `version_rollback_locked`). A platform
  administrator may force one — only an administrator, and only with a
  reason (`version_lock_force_not_admin`,
  `version_lock_force_needs_reason`) — and the reason is recorded with the
  switch.

---

## 9. Who can see it

### 9.1 Review

An upload starts `pending`. A reviewer — a platform administrator, a
competition's organiser, or someone the organiser named a reviewer —
approves or rejects it; a rejection carries a note. Approving does not
publish it.

A competition may review **automatically** instead: it decides on the
smoke (§6.4) — `solved` approves, publishes and curates the upload —
and asks three more things of an upload, each a refusal:

- something in the package runs, so there is something to smoke
  (`smoke_nothing_to_run`);
- the package is not byte for byte one already uploaded or already in the
  catalog (`duplicate_of_upload`, `duplicate_of_catalog`);
- where the competition has a model read every upload for text aimed at
  the agents that will solve it, the package is small enough to be read
  whole (`authoring_too_large_to_review`), and holds no such text the
  model can quote (`authoring_prompt_injection`).

### 9.2 Publishing

Publishing writes the package into the platform's catalog — the files on
disk are the publication — and is a separate step from approving
(`publish_*`). It requires an approved upload and, where the package
runs anything, the review of §7.5.

An organiser of a competition curating it, or an administrator, may take
a published upload down, with a reason that is recorded
(`unpublish_*`), and restore it later (`republish_*`). Taking it down
leaves the upload's row, so restoring it needs no new upload.

### 9.3 Into a competition

A published upload is played in a competition that **curates** it —
lists it among its challenges (`curation_*`). A competition whose set is
a whole catalog directory cannot curate one by one
(`curation_bucket_competition`), and neither can a competition whose set
follows another competition's, which never inherits player uploads
(`curation_followed_competition`). While a competition that locks its
challenges' versions is in play, adding a challenge to it or taking one
out needs a confirmation and a reason, recorded per challenge
(`curation_locked_needs_reason`).

### 9.4 The public catalog

Published is not public. Until it is **released to the catalog**
(`catalog_release_*`) an upload is seen only inside the competitions
curating it: by their organisers, by its authors, by administrators,
and by registered players once that competition has started. Released,
it joins the catalog anyone can practise from.

### 9.5 The evaluation pool, and what never leaves

An original upload whose authors allowed it to be used for evaluating
AI agents may choose the destination `eval_only`
(`destination_needs_eval_use`, `destination_needs_original`). It is then
kept out of the catalog, the credits and the public statistics; it
starts only in a competition that is not public and curates it, and its
files are read only by administrators, its authors, and that
competition's members.

Whatever the destination, a team sees what the package serves —
`attachments/`, the description, the prompts and its own box — and never
the package's source. Minted answers never leave the platform.

### 9.6 Credit

An upload names its uploader, by display name, only when they chose to
be credited. The name is withheld while a competition the reader can see
is playing it live, and wherever the upload's own event withholds that
entrant's identity.

### 9.7 Declarations

Where a competition names a contributor agreement, an upload declares
the licence it grants (`declaration_license`), whether it is original or
a port (`declaration_provenance`) — a port names its source as an
`http(s)` address (`declaration_source_missing`,
`declaration_source_not_web`), and an original names none
(`declaration_source_not_a_port`) — and whether AI helped write it
(`declaration_ai_assisted`). An upload may not grant more than the
team's captain granted when accepting the agreement
(`declaration_grant_beyond_team`). A declaration nobody made is stored
as *not declared*, never as an answer.

### 9.8 Into the corpus

An upload may graduate into the platform's curated corpus when it is
approved and published, its own solution solved it on the last smoke,
its licence allows redistribution, and it is not in the evaluation pool
(`graduation_*`). Its tree must not carry its own root `LICENSE` or
`CREDITS.md`, which graduation writes (`graduation_file_collision`).

---

## Appendix

### A. Audit rule codes

Every code the audit can report, and the clause it comes from. Each row
is linkable as `#<CODE>`.

| code | severity | clause | rule |
|---|---|---|---|
| `META000` | error | [§2.1](#2-1-top-level-keys) | `metadata.yaml` is readable YAML and a mapping |
| `META002` | error | [§2.1](#2-1-top-level-keys) | the required keys are present and typed |
| `META003` | error | [§2.1](#2-1-top-level-keys) | `difficulty` is `easy`, `medium` or `hard` |
| `META004` | error | [§2.1](#2-1-top-level-keys) | `tags` is a non-empty list of strings |
| `META005` | error | [§2.1](#2-1-top-level-keys) | no unknown or retired top-level key |
| `META006` | error | [§2.2](#2-2-questions) | each question's keys, id and shape |
| `META007` | warning | [§2.1](#2-1-top-level-keys) | the description names no bug class, technique or endpoint |
| `META008` | error | [§2.3](#2-3-how-a-question-s-mode-is-inferred) | the question has a mode |
| `META009` | error | [§2.5](#2-5-requires) | `requires:` resolves and has no cycle |
| `META010` | error | [§2.5](#2-5-requires) | the description does not paraphrase a gated prompt |
| `META011` | error | [§2.1](#2-1-top-level-keys) | `category` is known |
| `META012` | error | [§2.1](#2-1-top-level-keys) | `network_topology` is `simple` or `engagement` |
| `META013` | error | [§2.1](#2-1-top-level-keys) | `entry_urls` are absolute `http`, `https` or `tcp` URLs |
| `META014` | error | [§2.6](#2-6-prompts-and-per-instance-values) | a text binds only `param` |
| `META015` | error | [§5.1](#5-1-match) | a `match:` mode is known, `contains` is seeded, `judge` has `check:` |
| `META016` | error | [§2.1](#2-1-top-level-keys) | `vpn_backend` is known and only with `engagement` |
| `META017` | error | [§3.6](#3-6-network-topology) | an engagement host resolves through the tunnel |
| `META018` | error | [§2.2](#2-2-questions) | `scores:` is `capability` or `reporting` |
| `META019` | error | [§3.6](#3-6-network-topology) | an engagement host's top-level domain is reserved |
| `META020` | error | [§2.8](#2-8-declaring-the-standard) | `standard:` names a version the platform checks against |
| `STRUCT001` | error | [§1.4](#1-4-what-a-category-requires) | `docker-compose.yml` exists for `web` and `pwn` |
| `STRUCT002` | error | [§6.1](#6-1-the-solution) | a reference solution exists |
| `STRUCT003` | error | [§1.2](#1-2-files-the-platform-reads) | `services/` exists when compose builds |
| `STRUCT004` | error | [§1.2](#1-2-files-the-platform-reads) | each build context exists and has a `Dockerfile` |
| `STRUCT005` | error | [§3.4](#3-4-values-the-upload-checks) | a relative bind source exists |
| `STRUCT006` | error | [§4.2](#4-2-flag-rotator) | the rotator owns exactly the answers nothing else delivers |
| `STRUCT007` | error | [§4.2](#4-2-flag-rotator) | some service consumes the rotator |
| `STRUCT008` | error | [§4.2](#4-2-flag-rotator) | the rotator publishes no port |
| `STRUCT009` | error | [§4.2](#4-2-flag-rotator) | `/answers/<id>` is mounted only for dynamic ids |
| `STRUCT010` | error | [§1.4](#1-4-what-a-category-requires) | `attachments/` has a file for a file-shipping category |
| `STRUCT011` | error | [§4.3](#4-3-templates-attachments-tpl) | with no compose, a template delivers every dynamic answer |
| `STRUCT012` | error | [§3.6](#3-6-network-topology) | `ctfy.service.protocol` is `http` or `tcp` |
| `STRUCT013` | error | [§3.6](#3-6-network-topology) | a protocol label sits on a service with `ports:` |
| `STRUCT014` | error | [§3.6](#3-6-network-topology) | a `pwn` package labels a service `tcp` |
| `STRUCT015` | error | [§5.4](#5-4-checker-and-judge) | the judge service exists and names its answer |
| `STRUCT016` | error | [§5.4](#5-4-checker-and-judge) | the judge publishes no port |
| `STRUCT017` | error | [§4.3](#4-3-templates-attachments-tpl) | a template names only dynamic ids |
| `STRUCT018` | error | [§4.3](#4-3-templates-attachments-tpl) | a template parses |
| `STRUCT019` | error | [§4.4](#4-4-attachment-builder) | the builder carries the `ctfy-builder` profile |
| `STRUCT020` | error | [§4.4](#4-4-attachment-builder) | the builder publishes no port |
| `STRUCT021` | error | [§4.4](#4-4-attachment-builder) | the builder binds `./attachments` to `/out` |
| `STRUCT022` | error | [§3.6](#3-6-network-topology) | a raw TCP listener is labelled `tcp` |
| `COMPOSE001` | error | [§3.8](#3-8-what-else-the-audit-reads-in-compose) | `services:` is a mapping |
| `COMPOSE002` | error | [§3.2](#3-2-top-level-keys) | no top-level `version:` |
| `COMPOSE003` | error | [§3.6](#3-6-network-topology) | some service publishes a port |
| `COMPOSE004` | error | [§4.6](#4-6-what-may-never-carry-an-answer) | no Dockerfile but the rotator's bakes an answer |
| `COMPOSE005` | error | [§4.6](#4-6-what-may-never-carry-an-answer) | no build argument is named `ANSWER_*` |
| `COMPOSE006` | error | [§3.5](#3-5-resources-and-health) | every service has CPU and memory limits |
| `COMPOSE007` | error | [§3.8](#3-8-what-else-the-audit-reads-in-compose) | no service hard-codes its own public URL |
| `COMPOSE008` | error | [§3.8](#3-8-what-else-the-audit-reads-in-compose) | every `COPY`/`ADD` source exists |
| `COMPOSE009` | error | [§3.8](#3-8-what-else-the-audit-reads-in-compose) | a `RUN` that starts a database stops it |
| `COMPOSE010` | error | [§7.1](#7-1-the-network) | `x-ctfy-internet` is well formed |
| `COMPOSE011` | error | [§7.6](#7-6-sharing-one-environment) | `x-ctfy-sharing` is `shareable` or `per-connection` |
| `HEALTH001` | error | [§3.5](#3-5-resources-and-health) | every service has a health check |
| `NET001` | warning | [§3.6](#3-6-network-topology) | an engagement package's own VPN service is redundant |
| `NET002` | error | [§3.6](#3-6-network-topology) | that VPN service builds from the platform's base image |
| `NET003` | error | [§3.6](#3-6-network-topology) | that VPN service publishes its UDP port |
| `NET004` | error | [§3.6](#3-6-network-topology) | a WireGuard service sets `net.ipv4.ip_forward` |
| `NET005` | error | [§3.6](#3-6-network-topology) | no other service publishes a port |
| `AWD001` | error | [§3.9](#3-9-attack-defence-gameboxes) | `awd:` parses and declares a service |
| `AWD002` | error | [§3.9](#3-9-attack-defence-gameboxes) | the checker publishes no port |
| `AWD003` | error | [§3.9](#3-9-attack-defence-gameboxes) | both declared networks exist |
| `AWD004` | error | [§3.9](#3-9-attack-defence-gameboxes) | no privileged service, docker socket or host network |
| `AWD005` | error | [§3.9](#3-9-attack-defence-gameboxes) | every service is limited, and the box within 2 048 MiB |
| `AWD006` | error | [§3.9](#3-9-attack-defence-gameboxes) | every attackable service is on the game network |
| `AWD007` | error | [§3.9](#3-9-attack-defence-gameboxes) | `sshd` is on the management network only |
| `AWD008` | error | [§3.9](#3-9-attack-defence-gameboxes) | no service publishes a port |
| `AWD009` | error | [§3.9](#3-9-attack-defence-gameboxes) | each service's `answer_id` is a distinct declared question |
| `AWD010` | error | [§3.9](#3-9-attack-defence-gameboxes) | every attackable service builds from an `awd-base` image |
| `PATCH001` | error | [§3.9](#3-9-attack-defence-gameboxes) | `patch:` parses and its targets exist |
| `PATCH002` | error | [§3.9](#3-9-attack-defence-gameboxes) | its targets stay inside the package |
| `PATCH003` | error | [§3.9](#3-9-attack-defence-gameboxes) | its targets exclude `metadata.yaml`, `solution/` and the checker |
| `PATCH004` | error | [§3.9](#3-9-attack-defence-gameboxes) | its `live:` shell names a real service and directory |
| `PATCH005` | error | [§3.9](#3-9-attack-defence-gameboxes) | `patch:` requires `awd:` |
| `PATCH006` | error | [§3.9](#3-9-attack-defence-gameboxes) | the `live:` shell is in a scored service and says how to restart it |
| `EXPLOIT001` | error | [§6.1](#6-1-the-solution) | `exploit.py` parses |
| `EXPLOIT002` | error | [§6.1](#6-1-the-solution) | it imports `ExploitBase` and `run_exploit` |
| `EXPLOIT003` | error | [§6.1](#6-1-the-solution) | it defines `Exploit(ExploitBase)` with `run` |
| `EXPLOIT004` | error | [§6.1](#6-1-the-solution) | it calls `run_exploit` under `__main__` |
| `EXPLOIT007` | error | [§6.1](#6-1-the-solution) | it reads no answer in its own process |
| `SOURCE001` | error | [§4.6](#4-6-what-may-never-carry-an-answer) | no literal `FLAG{…}` in `services/` |
| `SOURCE002` | warning | [§2.1](#2-1-top-level-keys) | no vulnerability hint in source comments |
| `SOURCE003` | warning | [§2.1](#2-1-top-level-keys) | no vulnerability hint in rendered text |
| `SOURCE004` | warning | [§2.1](#2-1-top-level-keys) | no discovery shortcut route (`/docs`, `/swagger`, …) |
| `SOURCE005` | error | [§4.5](#4-5-seeder-and-params) | the seeder's and the judge's Python parses |
| `SOURCE006` | warning | [§7.1](#7-1-the-network) | runtime code dials no public host |
| `METADATA998` | error | [Appendix](#appendix) | the metadata checks timed out; the package is not clean |
| `METADATA999` | error | [Appendix](#appendix) | the metadata checks crashed; the package is not clean |
| `STRUCTURE998` | error | [Appendix](#appendix) | the structure checks timed out |
| `STRUCTURE999` | error | [Appendix](#appendix) | the structure checks crashed |
| `COMPOSE998` | error | [Appendix](#appendix) | the compose checks timed out |
| `COMPOSE999` | error | [Appendix](#appendix) | the compose checks crashed |
| `HEALTHCHECK998` | error | [Appendix](#appendix) | the health-check checks timed out |
| `HEALTHCHECK999` | error | [Appendix](#appendix) | the health-check checks crashed |
| `NETWORK998` | error | [Appendix](#appendix) | the network checks timed out |
| `NETWORK999` | error | [Appendix](#appendix) | the network checks crashed |
| `AWD998` | error | [Appendix](#appendix) | the gamebox checks timed out |
| `AWD999` | error | [Appendix](#appendix) | the gamebox checks crashed |
| `EXPLOIT998` | error | [Appendix](#appendix) | the solution checks timed out |
| `EXPLOIT999` | error | [Appendix](#appendix) | the solution checks crashed |
| `SOURCE998` | error | [Appendix](#appendix) | the source checks timed out |
| `SOURCE999` | error | [Appendix](#appendix) | the source checks crashed |

The `…998` and `…999` codes are the audit's own: one group of checks ran
out of time (30 seconds) or crashed on this package. The package is not
clean — that group did not look at it — and the other groups still
report.

### B. Upload refusal codes

Every code an upload — or a later step on it — is refused with, and the
clause it comes from. Each row is linkable as `#<code>`.

| code | clause |
|---|---|
| `archive_empty` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `archive_too_many_entries` | [§7.4](#7-4-size-limits) |
| `archive_member_not_a_file` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `archive_member_too_large` | [§7.4](#7-4-size-limits) |
| `archive_unpacks_too_large` | [§7.4](#7-4-size-limits) |
| `archive_no_metadata` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `attachment_hidden` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `attachment_too_deep` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `attachment_name_unusable` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `attachments_too_many` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `path_empty` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `path_absolute` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `path_home_directory` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `path_escapes` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `path_dot_segment` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `path_backslash_or_nul` | [§1.5](#1-5-what-an-upload-refuses-about-the-archive) |
| `compose_unparseable` | [§3.1](#3-1-which-file-and-when-it-is-ruled-on) |
| `compose_not_a_mapping` | [§3.1](#3-1-which-file-and-when-it-is-ruled-on) |
| `compose_top_level_key` | [§3.2](#3-2-top-level-keys) |
| `compose_external_network` | [§3.2](#3-2-top-level-keys) |
| `compose_container_name` | [§3.3](#3-3-service-keys) |
| `compose_extra_hosts` | [§3.3](#3-3-service-keys) |
| `compose_service_key` | [§3.3](#3-3-service-keys) |
| `compose_capability` | [§3.4](#3-4-values-the-upload-checks) |
| `compose_sysctl` | [§3.4](#3-4-values-the-upload-checks) |
| `compose_label` | [§3.4](#3-4-values-the-upload-checks) |
| `compose_network_mode` | [§3.4](#3-4-values-the-upload-checks) |
| `compose_volume_type` | [§3.4](#3-4-values-the-upload-checks) |
| `compose_bind_outside` | [§3.4](#3-4-values-the-upload-checks) |
| `compose_build_outside` | [§3.4](#3-4-values-the-upload-checks) |
| `compose_build_key` | [§3.4](#3-4-values-the-upload-checks) |
| `compose_build_path_outside` | [§3.4](#3-4-values-the-upload-checks) |
| `compose_build_network` | [§3.4](#3-4-values-the-upload-checks) |
| `authoring_memory_over_budget` | [§3.5](#3-5-resources-and-health) |
| `authoring_cpu_over_budget` | [§3.5](#3-5-resources-and-health) |
| `authoring_resources_undeclared` | [§3.5](#3-5-resources-and-health) |
| `declaration_license` | [§9.7](#9-7-declarations) |
| `declaration_provenance` | [§9.7](#9-7-declarations) |
| `declaration_ai_assisted` | [§9.7](#9-7-declarations) |
| `declaration_source_missing` | [§9.7](#9-7-declarations) |
| `declaration_source_not_web` | [§9.7](#9-7-declarations) |
| `declaration_source_not_a_port` | [§9.7](#9-7-declarations) |
| `declaration_grant_beyond_team` | [§9.7](#9-7-declarations) |
| `destination_needs_eval_use` | [§9.5](#9-5-the-evaluation-pool-and-what-never-leaves) |
| `destination_needs_original` | [§9.5](#9-5-the-evaluation-pool-and-what-never-leaves) |
| `standing_needs_minted_answer` | [§2.7](#2-7-what-an-upload-requires-of-the-questions) |
| `authoring_requires_minted_answer` | [§2.7](#2-7-what-an-upload-requires-of-the-questions) |
| `authoring_requires_a_question` | [§2.7](#2-7-what-an-upload-requires-of-the-questions) |
| `authoring_answer_undelivered` | [§2.7](#2-7-what-an-upload-requires-of-the-questions) |
| `standard_unsupported` | [§2.8](#2-8-declaring-the-standard) |
| `authoring_name_too_long` | [§2.1](#2-1-top-level-keys) |
| `authoring_name_unusable` | [§2.1](#2-1-top-level-keys) |
| `duplicate_of_upload` | [§9.1](#9-1-review) |
| `duplicate_of_catalog` | [§9.1](#9-1-review) |
| `smoke_nothing_to_run` | [§9.1](#9-1-review) |
| `authoring_too_large_to_review` | [§9.1](#9-1-review) |
| `authoring_prompt_injection` | [§9.1](#9-1-review) |
| `revision_drops_question` | [§8.3](#8-3-revising-an-approved-upload) |
| `publish_id_unusable` | [§9.2](#9-2-publishing) |
| `publish_not_approved` | [§9.2](#9-2-publishing) |
| `publish_no_files` | [§9.2](#9-2-publishing) |
| `publish_no_metadata` | [§9.2](#9-2-publishing) |
| `publish_id_taken` | [§9.2](#9-2-publishing) |
| `publish_exec_surface_unreviewed` | [§7.5](#7-5-build-time-and-the-review-of-what-a-package-runs) |
| `curation_bucket_competition` | [§9.3](#9-3-into-a-competition) |
| `curation_followed_competition` | [§9.3](#9-3-into-a-competition) |
| `curation_not_published` | [§9.3](#9-3-into-a-competition) |
| `curation_eval_pool_public` | [§9.5](#9-5-the-evaluation-pool-and-what-never-leaves) |
| `curation_released_elsewhere` | [§9.3](#9-3-into-a-competition) |
| `curation_locked_needs_reason` | [§9.3](#9-3-into-a-competition) |
| `catalog_release_not_published` | [§9.4](#9-4-the-public-catalog) |
| `catalog_release_not_approved` | [§9.4](#9-4-the-public-catalog) |
| `catalog_release_eval_pool` | [§9.5](#9-5-the-evaluation-pool-and-what-never-leaves) |
| `catalog_release_needs_content_review` | [§9.4](#9-4-the-public-catalog) |
| `catalog_release_needs_exec_review` | [§7.5](#7-5-build-time-and-the-review-of-what-a-package-runs) |
| `catalog_release_not_curated_here` | [§9.4](#9-4-the-public-catalog) |
| `unpublish_id_unusable` | [§9.2](#9-2-publishing) |
| `unpublish_not_on_disk` | [§9.2](#9-2-publishing) |
| `unpublish_needs_reason` | [§9.2](#9-2-publishing) |
| `unpublish_not_curated_here` | [§9.2](#9-2-publishing) |
| `republish_not_withdrawn` | [§9.2](#9-2-publishing) |
| `republish_not_curated_here` | [§9.2](#9-2-publishing) |
| `revision_already_live` | [§8.3](#8-3-revising-an-approved-upload) |
| `revision_not_approved` | [§8.3](#8-3-revising-an-approved-upload) |
| `revision_upload_not_approved` | [§8.3](#8-3-revising-an-approved-upload) |
| `revision_apply_drops_question` | [§8.3](#8-3-revising-an-approved-upload) |
| `revision_curated_into_standing` | [§8.3](#8-3-revising-an-approved-upload) |
| `revision_exec_surface_unreviewed` | [§7.5](#7-5-build-time-and-the-review-of-what-a-package-runs) |
| `revision_changed_while_applying` | [§8.3](#8-3-revising-an-approved-upload) |
| `revision_not_on_disk` | [§8.3](#8-3-revising-an-approved-upload) |
| `graduation_not_approved` | [§9.8](#9-8-into-the-corpus) |
| `graduation_not_published` | [§9.8](#9-8-into-the-corpus) |
| `graduation_smoke_not_solved` | [§9.8](#9-8-into-the-corpus) |
| `graduation_never_smoked` | [§9.8](#9-8-into-the-corpus) |
| `graduation_no_license` | [§9.8](#9-8-into-the-corpus) |
| `graduation_license_not_redistributable` | [§9.8](#9-8-into-the-corpus) |
| `graduation_eval_pool` | [§9.8](#9-8-into-the-corpus) |
| `graduation_file_collision` | [§9.8](#9-8-into-the-corpus) |
| `version_not_withdrawable` | [§8.1](#8-1-every-upload-is-a-version) |
| `version_tree_not_kept` | [§8.1](#8-1-every-upload-is-a-version) |
| `switch_not_approved` | [§8.4](#8-4-which-version-players-get) |
| `switch_already_live` | [§8.4](#8-4-which-version-players-get) |
| `switch_drops_question` | [§8.4](#8-4-which-version-players-get) |
| `switch_needs_reason` | [§8.4](#8-4-which-version-players-get) |
| `switch_exec_surface_unreviewed` | [§8.4](#8-4-which-version-players-get) |
| `switch_never_ends` | [§8.4](#8-4-which-version-players-get) |
| `switch_already_scheduled` | [§8.4](#8-4-which-version-players-get) |
| `switch_changed` | [§8.4](#8-4-which-version-players-get) |
| `switch_upload_not_approved` | [§8.4](#8-4-which-version-players-get) |
| `switch_curated_into_standing` | [§8.4](#8-4-which-version-players-get) |
| `version_switch_locked` | [§8.4](#8-4-which-version-players-get) |
| `version_rollback_locked` | [§8.4](#8-4-which-version-players-get) |
| `version_lock_force_not_admin` | [§8.4](#8-4-which-version-players-get) |
| `version_lock_force_needs_reason` | [§8.4](#8-4-which-version-players-get) |
