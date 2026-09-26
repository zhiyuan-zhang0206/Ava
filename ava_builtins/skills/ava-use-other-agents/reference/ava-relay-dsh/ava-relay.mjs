// Ava impersonation relay for DeepSeek Harness (dsh), as a Cordis plugin.
//
// Load it into the dsh process whose session takes over an Ava agent (see
// conventions/agent-impersonation-hosts.md, "DeepSeek Harness"). Every model
// shell command receives DSH_AVA_RELAY_STUB: a per-session path inside a
// private directory this plugin owns. `ava impersonate request --provider dsh`
// writes the scoped relay credential there (0600) instead of printing it,
// because dsh uploads session logs with its model requests by default. A
// one-second poller consumes each stub once, starts `ava impersonate relay
// --provider dsh` as a background job owned by that session, and steers every
// JSON line the relay prints into the session: an idle session starts a turn,
// a busy one takes the message at its next step.
//
// With `takeoverFile` configured (the self-takeover launcher, spawn_dsh.py),
// the plugin also stands in for the one-shot headless runner: it creates one
// session, submits the launch message read (and deleted) from that file, echoes
// the session to stdout, and keeps serving it until the process is stopped.
//
// Plain ESM on Node built-ins only: a plugin loaded by absolute path cannot
// resolve dsh's own packages. Verified with dsh 0.1.5-rc.3.
import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import { mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, unlinkSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createInterface } from 'node:readline'

export const name = 'ava-relay'
export const inject = ['agents', 'jobs', 'shellEnv']

const PLUGIN = 'ava-relay'
const STUB_KEYS = ['SID', 'AGENT', 'AVA_IMPERSONATION_RELAY_TOKEN', 'AVA_IMPERSONATION_RELAY_PY']
const STUB_SUFFIX = '.env'
const POLL_MS = 1000
const STDERR_TAIL_CHARS = 8000
// dsh bounds a notice's one-line summary to 120 characters.
const SUMMARY_MAX_CHARS = 120

function userMessage(text, source) {
  return Object.freeze({
    id: randomUUID(),
    role: 'user',
    content: Object.freeze([Object.freeze({ type: 'text', text })]),
    source,
  })
}

/** A pushed envelope as a `notice`: the collapsed transcript row shows its first line. */
function relayMessage(text) {
  const first = text.split('\n', 1)[0].trim() || 'Ava relay message'
  const summary = first.length <= SUMMARY_MAX_CHARS ? first : `${first.slice(0, SUMMARY_MAX_CHARS - 1)}…`
  return userMessage(text, Object.freeze({ kind: 'plugin', plugin: PLUGIN, form: 'notice', summary }))
}

/** Claim a stub by rename (one consumer wins), read it, and delete it. */
function consumeStub(path) {
  const claimed = `${path}.${process.pid}`
  try {
    renameSync(path, claimed)
  } catch (error) {
    if (error.code === 'ENOENT') return undefined
    throw error
  }
  const values = {}
  try {
    for (const line of readFileSync(claimed, 'utf8').split('\n')) {
      const at = line.indexOf('=')
      if (at > 0) values[line.slice(0, at)] = line.slice(at + 1)
    }
  } finally {
    unlinkSync(claimed)
  }
  for (const key of STUB_KEYS) {
    if (!values[key]) throw new Error(`ava-relay: stub ${path} lacks ${key}`)
  }
  return values
}

/** Run the relay as a background job owned by `agent`; each stdout line is one JSON string. */
function startRelay(ctx, agent, stub) {
  let stderr = ''
  return ctx.jobs.start({
    kind: 'ava-relay',
    label: `Ava agent ${stub.AGENT} impersonation session ${stub.SID} inbox relay`,
    owner: agent,
    run() {
      const child = spawn(
        stub.AVA_IMPERSONATION_RELAY_PY,
        ['-m', 'cli', 'impersonate', 'relay', stub.AGENT, '--session', stub.SID, '--provider', 'dsh'],
        {
          env: { ...process.env, AVA_IMPERSONATION_RELAY_TOKEN: stub.AVA_IMPERSONATION_RELAY_TOKEN },
          stdio: ['ignore', 'pipe', 'pipe'],
        },
      )
      const note = (text) => {
        stderr = (stderr + text).slice(-STDERR_TAIL_CHARS)
      }
      createInterface({ input: child.stdout }).on('line', (line) => {
        try {
          const text = JSON.parse(line)
          if (typeof text !== 'string') throw new Error(`relay line is not a JSON string: ${line}`)
          agent.steer(relayMessage(text))
        } catch (error) {
          // Stop the relay rather than the host: its heartbeat ends, and Ava
          // stops the takeover with the pending input preserved.
          note(`ava-relay: delivery failed: ${error}\n`)
          child.kill('SIGTERM')
        }
      })
      child.stderr.setEncoding('utf8').on('data', note)
      const done = new Promise((resolve) => {
        child.on('error', (error) => resolve({ status: 'failed', detail: String(error) }))
        child.on('close', (code, signal) => resolve(
          code === 0 ? { status: 'completed', detail: 'exit code: 0' }
            : signal !== null ? { status: 'killed', detail: `signal: ${signal}` }
              : { status: 'failed', detail: `exit code: ${code}` },
        ))
      })
      return {
        cancel() { child.kill('SIGTERM') },
        done,
        readOutput() {
          const out = stderr
          stderr = ''
          return out
        },
      }
    },
  })
}

function scanStubs(ctx, dir) {
  for (const entry of readdirSync(dir)) {
    if (!entry.endsWith(STUB_SUFFIX)) continue
    const agent = ctx.agents.get(entry.slice(0, -STUB_SUFFIX.length))
    if (agent === undefined) continue
    const stub = consumeStub(join(dir, entry))
    if (stub === undefined) continue
    const job = startRelay(ctx, agent, stub)
    console.error(`ava-relay: ${job} relays Ava agent ${stub.AGENT} session ${stub.SID} into ${agent.id}`)
  }
}

function echo(text) {
  process.stdout.write(text)
}

async function runTakeover(ctx, file) {
  const message = readFileSync(file, 'utf8')
  unlinkSync(file)
  await ctx.get('loader')?.await()
  const selection = ctx.get('agentDefaultModel').currentSelection()
  const { agent } = await ctx.agents.create({
    sessionId: `session-${randomUUID()}`,
    meta: { cwd: process.cwd() },
    agentOptions: { provider: selection.provider, model: selection.model },
  })
  ctx.on('agent/assistant-stream', ({ agent: subject, frame }) => {
    if (subject !== agent) return
    if (frame.type === 'chunk' && frame.chunk.type === 'text-delta') echo(frame.chunk.text)
    if (frame.type === 'end') echo('\n')
  })
  ctx.on('session/event', (session, event) => {
    if (session !== agent.session) return
    if (event.type === 'tool/call') {
      echo(`[tool] ${event.data.name} ${String(event.data.arguments).slice(0, 400)}\n`)
    } else if (event.type === 'user/message' && event.data.source?.plugin === PLUGIN) {
      echo(`[ava-relay] ${event.data.content.map((block) => block.text ?? '').join(' ').slice(0, 400)}\n`)
    } else if (event.type === 'turn/end' && event.data.reason.kind !== 'completed') {
      const { reason } = event.data
      echo(`[turn ${reason.kind}]${reason.kind === 'error' ? ` ${reason.error.code}: ${reason.error.message}` : ''}\n`)
    }
  })
  echo(`dsh takeover session ${agent.id} (${selection.provider}/${selection.model})\n`)
  agent.followup(userMessage(message, Object.freeze({ kind: 'user' })))
}

export function apply(ctx, config) {
  const dir = mkdtempSync(join(tmpdir(), 'ava-relay-dsh-'))
  ctx.shellEnv.register({
    name: PLUGIN,
    variables: {
      DSH_AVA_RELAY_STUB: {
        description: 'Path where `ava impersonate request --provider dsh` leaves the Ava relay credential for this session.',
      },
    },
    resolve: (execution) => (execution.agent === undefined
      ? {}
      : { DSH_AVA_RELAY_STUB: join(dir, `${execution.agent.session.header.id}${STUB_SUFFIX}`) }),
  })
  ctx.effect(() => {
    const timer = setInterval(() => {
      try {
        scanStubs(ctx, dir)
      } catch (error) {
        console.error('ava-relay: stub scan failed', error)
      }
    }, POLL_MS)
    return () => {
      clearInterval(timer)
      rmSync(dir, { recursive: true, force: true })
    }
  })
  if (config?.takeoverFile) {
    runTakeover(ctx, config.takeoverFile).catch((error) => {
      console.error('ava-relay: takeover session failed', error)
      ctx.get('appExit')(1)
    })
  }
}
