# Architecture and failure behavior

The bridge adapts explicitly configured native Codex App Servers. Each backend owns its
provider settings, credentials, process and session storage. The bridge registry names
destinations; it does not choose a substitute provider when a request fails.

| Module | Responsibility |
| --- | --- |
| `cli`, `tui`, `server`, `compat` | Human and MCP entry points, argument validation, caller context |
| `config`, `provisioning`, `providers` | Registry, daemon lifecycle, provider setup and launchers |
| `transport` | Bounded native JSON-RPC over a local Unix WebSocket, home identity verification |
| `sessions`, `messages`, `operations` | Provider-checked session operations, bounded text pagination, continuation locks |
| `delegation` | Caller identity, ancestry graph, task assignments and cycle/depth checks |
| `integration`, `installation`, `removal` | Persistent native registration, ownership tracking, scoped removal |
| `diagnostics` | Read-only installation and provider-binding checks |

CLI and TUI management share the same application functions. TUI session operations use an
operator guard; MCP mutations obtain the caller from native request metadata. The model cannot
supply a replacement caller identity in the public tool schema.
Tool argument validation preserves strings verbatim. The pinned MCP SDK's automatic JSON
pre-parsing is disabled per tool so nullable text fields and opaque JSON cursor strings retain
their original values; normal type validation still applies.

Native Unix sockets may live in directories longer than Linux's socket-address limit. Transport
connections and removal checks address these through an open directory descriptor under
`/proc/self/fd`, preserving the actual Codex home and avoiding temporary links or cwd changes.

Creating or forking a task validates the actual returned provider and selection before sending
its initial turn. The bridge records a new assignment with a unique ID, session type, task,
criteria and ancestry. Inherited instructions are history; the new assignment describes the
current task. State migration retains previous ancestry. Corrupt state prevents mutations and
is never silently replaced with an empty graph.

Continuing an idle session holds a per-home, per-thread process lock across read, resume and
turn start. A competing bridge continuation fails promptly. This coordinates bridge workers;
native clients still rely on native server concurrency rules. The bridge checks state again after
resume. Steering supplies the caller's expected active turn ID, allowing the daemon to reject
an outdated request without interrupting another turn.

Messages are selected newest first and displayed in chronological order within each page.
Only user and assistant text is returned. A text budget can divide an item across responses;
`text_offset` gives its character offset. Pass `next_cursor`, or a session turn's
`older_items_cursor`, unchanged to `read_messages`. Continuation tokens carry item positions
and a prefix hash, never the message text. They are bound to the home, session and turn. If
an item has been edited or moved outside the bounded history window, restart without a cursor.
`messages_omitted` marks a turn whose preview received no remaining text budget.

| Failure | Behavior |
| --- | --- |
| Disabled or mismatched provider | Reject task dispatch; never fall back |
| Lost reply after a native mutation | Report ambiguity; inspect state before retrying |
| Known thread returned as a new child | Reject it; never delete the existing thread |
| Failed installation build | Keep current application and shell configuration |
| Registry/ownership record corruption | Report repairable error; do not guess ownership |
| Partial daemon removal | Keep a disabled route so the operation can be retried |
| Edited launcher or bundled guide | Preserve user changes |
| Project uninstall | Remove owned application integration; preserve daemons and session storage |

Registry writes, delegation writes and ownership updates have process locks and atomic file
replacement. Installation also locks its application root and public command directory. Shell
profiles are re-read after dependency installation; owned insertions are recorded exactly for
later removal. Native registration and launcher installation share the application lock, so an
uninstall cannot remove the program between recording ownership and enabling its integration.
Daemon removal acquires that same application lock before its registry lock. Creation through
the installed command rejects daemon homes and registries inside the application directory,
including backends that have not yet connected to MCP.
Old application environments remain available to already-running MCP workers.

Native execution permissions remain with the target daemon. The bridge does not service
interactive native approvals. Connect/setup operations verify model access; diagnostics and
launcher installation do not make model requests.
After model verification, provider connection rechecks the launcher before enabling the route.
A launcher removed during verification is recreated; a differing replacement is preserved and
connection fails. TUI stop confirmation also checks that the selected backend still matches
the directory shown before executing the stop.
