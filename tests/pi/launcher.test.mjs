import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";
import {
  launch,
  parseArgs,
} from "../../src/devbox/assets/harnesses/pi/launcher.mjs";

function temp(t) {
  const dir = mkdtempSync(join(tmpdir(), "devbox-pi-test-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

function mockSdk(agentDir, run = async () => {}) {
  const calls = { models: [], services: [], sessions: [], disposed: 0 };
  const sdk = {
    getAgentDir: () => agentDir,
    ProjectTrustStore: class {
      get() {
        return null;
      }
    },
    hasTrustRequiringProjectResources: () => false,
    initTheme: (theme) => {
      calls.theme = theme;
    },
    SettingsManager: {
      create: () => ({
        getEnabledModels: () => undefined,
        getTheme: () => "dark",
        getDefaultProjectTrust: () => "ask",
        setProjectTrusted: (value) => {
          calls.trusted = value;
        },
      }),
    },
    SessionManager: {
      create: (...args) => ({ kind: "new", args }),
      forkFrom: (...args) => ({ kind: "fork", args }),
    },
    ModelRuntime: {
      create: async (options) => {
        calls.models.push(options);
        return options;
      },
    },
    createCodemodeExtension: () => () => {},
    createToolSearchExtension: () => () => {},
    createMcpExtension: () => () => {},
    createAgentSessionServices: async (options) => {
      calls.services.push(options);
      return { ...options, diagnostics: [] };
    },
    createAgentSessionFromServices: async (options) => {
      calls.sessions.push(options);
      return { session: {} };
    },
    createAgentSessionRuntime: async (factory, options) => {
      const created = await factory(options);
      return {
        ...created,
        replace: async (context = {}) =>
          factory({
            ...options,
            cwd: "/another-project",
            sessionStartEvent: {},
            ...context,
          }),
        dispose: async () => {
          calls.disposed++;
        },
      };
    },
    InteractiveMode: class {
      constructor(runtime, options) {
        this.runtime = runtime;
        calls.ui = options;
      }
      async run() {
        await run(this.runtime);
      }
    },
  };
  return { sdk, calls };
}

test("launcher parses only the supported interactive arguments", () => {
  const parsed = parseArgs([
    "--fork",
    "/saved.jsonl",
    "--append-system-prompt",
    "/instructions.md",
    "--no-approve",
  ]);
  assert.equal(parsed.fork, "/saved.jsonl");
  assert.deepEqual(parsed.prompts, ["/instructions.md"]);
  assert.equal(parsed.projectTrusted, false);
  for (const args of [
    ["--fork"],
    ["--append-system-prompt", "--fork"],
    ["--print"],
    ["--fork", "a", "--fork", "b"],
  ]) {
    assert.throws(() => parseArgs(args));
  }
});

test("shared auth and appended instructions survive session replacement", async (t) => {
  const dir = temp(t);
  const prompt = join(dir, "instructions.md");
  writeFileSync(prompt, "Devbox instructions");
  const { sdk, calls } = mockSdk(join(dir, "agent"), (runtime) =>
    runtime.replace(),
  );
  await launch(
    sdk,
    parseArgs(["--append-system-prompt", prompt]),
    {
      DEVBOX_SHARED_AUTH: join(dir, "shared"),
      PI_CODING_AGENT_SESSION_DIR: join(dir, "sessions"),
    },
    dir,
  );
  assert.equal(calls.models.length, 2);
  for (const options of calls.models)
    assert.equal(options.authPath, join(dir, "shared/auth.json"));
  for (const services of calls.services) {
    assert.equal(
      services.modelRuntime.modelsPath,
      join(dir, "agent/models.json"),
    );
    assert.deepEqual(services.resourceLoaderOptions.appendSystemPrompt, [
      "Devbox instructions",
    ]);
    assert.deepEqual(
      services.resourceLoaderOptions.extensionFactories.map((f) => f.name),
      ["codemode", "tool-search", "mcp"],
    );
  }
  assert.deepEqual(calls.sessions[0].sessionManager.args, [
    dir,
    join(dir, "sessions"),
  ]);
  assert.equal(calls.theme, "dark");
  assert.equal(calls.disposed, 1);
});

test("configured model scopes are retained across session replacement", async (t) => {
  const dir = temp(t);
  const { sdk, calls } = mockSdk(join(dir, "agent"), (runtime) =>
    runtime.replace(),
  );
  const createSettings = sdk.SettingsManager.create;
  sdk.SettingsManager.create = (...args) => ({
    ...createSettings(...args),
    getEnabledModels: () => ["openai/*"],
  });
  const scopes = [{ model: { id: "test" } }];
  sdk.resolveModelScopeWithDiagnostics = async (patterns) => {
    assert.deepEqual(patterns, ["openai/*"]);
    return { scopedModels: scopes, diagnostics: [] };
  };
  await launch(sdk, parseArgs([]), {}, dir);
  assert.ok(calls.sessions.every((session) => session.scopedModels === scopes));
});

test("without shared mount auth is instance-local", async (t) => {
  const dir = temp(t);
  const { sdk, calls } = mockSdk(join(dir, "agent"));
  await launch(sdk, parseArgs([]), {}, dir);
  assert.equal(calls.models[0].authPath, join(dir, "agent/auth.json"));
});

test("resuming forks rather than opening the original session", async (t) => {
  const dir = temp(t);
  const { sdk, calls } = mockSdk(join(dir, "agent"));
  await launch(
    sdk,
    parseArgs(["--fork", "/snapshot.jsonl"]),
    { PI_CODING_AGENT_SESSION_DIR: "/sessions" },
    dir,
  );
  assert.equal(calls.sessions[0].sessionManager.kind, "fork");
  assert.deepEqual(calls.sessions[0].sessionManager.args, [
    "/snapshot.jsonl",
    dir,
    "/sessions",
  ]);
});

test("explicit project trust refusal is respected", async (t) => {
  const dir = temp(t);
  const { sdk, calls } = mockSdk(join(dir, "agent"));
  await launch(sdk, parseArgs(["--no-approve", "--no-extensions"]), {}, dir);
  assert.equal(calls.trusted, false);
  assert.equal(calls.services[0].resourceLoaderOptions.noExtensions, true);
});

test("unapproved project resources are skipped in a headless launch", async (t) => {
  const dir = temp(t);
  const { sdk, calls } = mockSdk(join(dir, "agent"));
  sdk.hasTrustRequiringProjectResources = () => true;
  await launch(sdk, parseArgs([]), {}, dir);
  assert.equal(calls.trusted, false);
});

test("an undefined stored trust decision still asks", async (t) => {
  const dir = temp(t);
  let asked = false;
  const { sdk, calls } = mockSdk(join(dir, "agent"), (runtime) =>
    runtime.replace({
      projectTrustContext: {
        hasUI: true,
        ui: {
          confirm: async () => {
            asked = true;
            return true;
          },
        },
      },
    }),
  );
  sdk.ProjectTrustStore = class {
    get() {
      return undefined;
    }
  };
  sdk.hasTrustRequiringProjectResources = () => true;
  await launch(sdk, parseArgs([]), {}, dir);
  assert.equal(asked, true);
  assert.equal(calls.trusted, true);
});

test("switching workspace uses the active TUI for a trust question", async (t) => {
  const dir = temp(t);
  let asked = false;
  const { sdk, calls } = mockSdk(join(dir, "agent"), (runtime) =>
    runtime.replace({
      projectTrustContext: {
        hasUI: true,
        ui: {
          confirm: async () => {
            asked = true;
            return true;
          },
        },
      },
    }),
  );
  sdk.hasTrustRequiringProjectResources = () => true;
  await launch(sdk, parseArgs([]), {}, dir);
  assert.equal(asked, true);
  assert.equal(calls.trusted, true);
});

test("runtime is disposed when interactive startup fails", async (t) => {
  const dir = temp(t);
  const { sdk, calls } = mockSdk(join(dir, "agent"), async () => {
    throw new Error("UI failed");
  });
  await assert.rejects(launch(sdk, parseArgs([]), {}, dir), /UI failed/);
  assert.equal(calls.disposed, 1);
});

// Optional real-SDK checks: no container service or provider requests. Set this
// to Pi's installed dist/index.js to validate login, fork, and interprocess locks.
const sdkPath = process.env.DEVBOX_PI_SDK;
test(
  "real SDK persists shared login across independent runtimes and forks",
  { skip: !sdkPath },
  async (t) => {
    const dir = temp(t);
    const agent = join(dir, "agent");
    const sessions = join(dir, "sessions");
    process.env.PI_CODING_AGENT_DIR = agent;
    process.env.PI_CODING_AGENT_SESSION_DIR = sessions;
    process.env.PI_OFFLINE = "1";
    process.env.PI_SKIP_VERSION_CHECK = "1";
    const sdk = await import(pathToFileURL(sdkPath));
    assert.equal(typeof sdk.resolveModelScopeWithDiagnostics, "function");
    mkdirSync(agent, { recursive: true });
    writeFileSync(
      join(agent, "settings.json"),
      JSON.stringify({ enabledModels: ["openai/*"] }),
    );
    const env = { ...process.env, DEVBOX_SHARED_AUTH: join(dir, "shared") };
    const prompt = join(dir, "instructions.md");
    writeFileSync(prompt, "Unique Devbox integration instructions");
    const source = sdk.SessionManager.create(dir, sessions);
    source.appendMessage({
      role: "user",
      content: "Saved conversation",
      timestamp: Date.now(),
    });
    const sourceFile = source.getSessionFile();
    const sourceContent = readFileSync(sourceFile, "utf8");
    let launches = 0;
    const testSdk = {
      ...sdk,
      InteractiveMode: class {
        constructor(runtime) {
          this.runtime = runtime;
        }
        async run() {
          launches++;
          const runtime = this.runtime;
          assert.ok(
            runtime.session.systemPrompt.includes(
              "Unique Devbox integration instructions",
            ),
          );
          if (launches === 1) {
            assert.notEqual(runtime.session.sessionFile, sourceFile);
            assert.ok(
              runtime.session.messages.some((m) =>
                JSON.stringify(m).includes("Saved conversation"),
              ),
            );
            await runtime.services.modelRuntime.login("openai", "api_key", {
              prompt: async () => "devbox-test-key",
              notify: () => {},
            });
          }
          assert.deepEqual(
            await runtime.services.modelRuntime.listCredentials(),
            [{ providerId: "openai", type: "api_key" }],
          );
          await runtime.newSession();
          assert.ok(runtime.session.scopedModels.length > 0);
          assert.deepEqual(
            await runtime.services.modelRuntime.listCredentials(),
            [{ providerId: "openai", type: "api_key" }],
          );
        }
      },
    };
    await launch(
      testSdk,
      parseArgs([
        "--fork",
        sourceFile,
        "--append-system-prompt",
        prompt,
        "--no-extensions",
      ]),
      env,
      dir,
    );
    await launch(
      testSdk,
      parseArgs(["--append-system-prompt", prompt, "--no-extensions"]),
      env,
      dir,
    );
    assert.equal(readFileSync(sourceFile, "utf8"), sourceContent);
    assert.equal(statSync(join(dir, "shared/auth.json")).mode & 0o777, 0o600);
    assert.throws(() => statSync(join(agent, "auth.json")), { code: "ENOENT" });
  },
);

test(
  "real SDK serializes OAuth refresh across independent processes",
  { skip: !sdkPath },
  async (t) => {
    const dir = temp(t);
    const shared = join(dir, "auth");
    mkdirSync(shared, { mode: 0o700 });
    writeFileSync(
      join(shared, "auth.json"),
      JSON.stringify({
        "devbox-refresh-test": {
          type: "oauth",
          access: "expired",
          refresh: "initial",
          expires: 0,
        },
      }),
      { mode: 0o600 },
    );
    const worker = fileURLToPath(
      new URL("./refresh-worker.mjs", import.meta.url),
    );
    const countFile = join(dir, "refreshes");
    await Promise.all(
      Array.from(
        { length: 4 },
        (_, i) =>
          new Promise((resolve, reject) => {
            const child = spawn(
              process.execPath,
              [worker, sdkPath, shared, countFile],
              {
                env: {
                  ...process.env,
                  PI_CODING_AGENT_DIR: join(dir, `agent-${i}`),
                  PI_OFFLINE: "1",
                },
                stdio: ["ignore", "pipe", "pipe"],
              },
            );
            let output = "";
            child.stdout.on("data", (data) => {
              output += data;
            });
            child.stderr.on("data", (data) => {
              output += data;
            });
            child.on("error", reject);
            child.on("exit", (code) =>
              code === 0
                ? resolve()
                : reject(new Error(output || `worker exited ${code}`)),
            );
          }),
      ),
    );
    assert.equal(readFileSync(countFile, "utf8"), "refresh\n");
    const stored = JSON.parse(readFileSync(join(shared, "auth.json"), "utf8"));
    assert.equal(stored["devbox-refresh-test"].refresh, "rotated");
    assert.throws(() => statSync(join(shared, "auth.json.lock")), {
      code: "ENOENT",
    });
  },
);
