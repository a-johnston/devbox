#!/usr/bin/env node
/**
 * Devbox's interactive Pi entry point. Keep credentials in Pi's native locked
 * store, independently of the per-instance agent directory. This is deliberately
 * not a replacement for the full Pi CLI (package management, RPC, print mode).
 */
import { readFileSync, realpathSync } from "node:fs";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline/promises";
import { fileURLToPath } from "node:url";

const HELP = `Usage: devbox-pi [--append-system-prompt FILE] [--fork SESSION]

Starts Pi's interactive UI with Devbox's shared credential store.
DEVBOX_SHARED_AUTH selects the directory containing auth.json and its lock.
Without it, credentials stay in Pi's normal, instance-local agent directory.

Options:
  --append-system-prompt FILE  Append instructions (repeatable)
  --fork SESSION              Fork a saved conversation into the current workspace
  --approve / --no-approve    Allow / skip protected project configuration
  --offline                   Disable startup network checks
  --no-extensions              Disable extensions, including built-ins
  --help, -h                  Show this help

Use the ordinary pi command for package management and other CLI modes.
`;

export function parseArgs(args) {
  const options = { prompts: [] };
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--help" || arg === "-h") options.help = true;
    else if (arg === "--offline") options.offline = true;
    else if (arg === "--no-extensions") options.noExtensions = true;
    else if (arg === "--approve") options.projectTrusted = true;
    else if (arg === "--no-approve") options.projectTrusted = false;
    else if (arg === "--append-system-prompt" || arg === "--fork") {
      const value = args[++i];
      if (!value || value.startsWith("--"))
        throw new Error(`${arg} requires a file path`);
      if (arg === "--append-system-prompt")
        options.prompts.push(resolve(value));
      else {
        if (options.fork) throw new Error("--fork may only be supplied once");
        options.fork = resolve(value);
      }
    } else
      throw new Error(`Unsupported argument: ${arg}. Run devbox-pi --help.`);
  }
  return options;
}

async function askProjectTrust(cwd) {
  if (!process.stdin.isTTY) return false;
  const input = createInterface({
    input: process.stdin,
    output: process.stdout,
  });
  try {
    const answer = await input.question(
      `Load project settings and executable extensions from ${cwd}? [y/N] `,
    );
    return /^(y|yes)$/i.test(answer.trim());
  } finally {
    input.close();
  }
}

// Exported so tests can exercise launch/replacement wiring without a Pi install
// or a model service. The executable below supplies the actual Pi SDK.
export async function launch(
  sdk,
  options,
  env = process.env,
  cwd = process.cwd(),
) {
  const agentDir = sdk.getAgentDir();
  const authPath = join(
    env.DEVBOX_SHARED_AUTH ? resolve(env.DEVBOX_SHARED_AUTH) : agentDir,
    "auth.json",
  );
  const appendSystemPrompt = options.prompts.map((file) =>
    readFileSync(file, "utf8"),
  );
  const sessionDir = env.PI_CODING_AGENT_SESSION_DIR;
  const sessionManager = options.fork
    ? sdk.SessionManager.forkFrom(options.fork, cwd, sessionDir)
    : sdk.SessionManager.create(cwd, sessionDir);
  const trustStore = new sdk.ProjectTrustStore(agentDir);
  const trustDecisions = new Map();

  const createRuntime = async ({
    cwd,
    agentDir,
    sessionManager,
    sessionStartEvent,
    projectTrustContext,
  }) => {
    const settingsManager = sdk.SettingsManager.create(cwd, agentDir, {
      projectTrusted: false,
    });
    let projectTrusted = options.projectTrusted ?? trustDecisions.get(cwd);
    if (projectTrusted === undefined) {
      projectTrusted =
        !sdk.hasTrustRequiringProjectResources(cwd) || trustStore.get(cwd);
      // A missing decision may be null or undefined; neither is a refusal.
      if (projectTrusted == null) {
        const policy = settingsManager.getDefaultProjectTrust();
        const confirm = () =>
          projectTrustContext?.hasUI
            ? projectTrustContext.ui.confirm(
                "Trust project?",
                `Load settings and executable extensions from ${cwd}?`,
              )
            : sessionStartEvent
              ? false
              : askProjectTrust(cwd);
        projectTrusted =
          policy === "always" || (policy !== "never" && (await confirm()));
      }
      trustDecisions.set(cwd, projectTrusted);
    }
    settingsManager.setProjectTrusted(projectTrusted);
    // Recreated for /new, /resume and /fork too: never fall back to local auth.
    const modelRuntime = await sdk.ModelRuntime.create({
      authPath,
      modelsPath: join(agentDir, "models.json"),
    });
    const services = await sdk.createAgentSessionServices({
      cwd,
      agentDir,
      modelRuntime,
      settingsManager,
      resourceLoaderOptions: {
        appendSystemPrompt,
        noExtensions: options.noExtensions,
        extensionFactories: [
          {
            name: "codemode",
            factory: sdk.createCodemodeExtension(),
            builtin: true,
            replaceable: true,
          },
          {
            name: "tool-search",
            factory: sdk.createToolSearchExtension(),
            builtin: true,
            replaceable: true,
          },
          {
            name: "mcp",
            factory: sdk.createMcpExtension(),
            builtin: true,
            replaceable: true,
          },
        ],
      },
    });
    const modelPatterns = settingsManager.getEnabledModels();
    const scope = modelPatterns?.length
      ? await sdk.resolveModelScopeWithDiagnostics(modelPatterns, modelRuntime)
      : { scopedModels: [], diagnostics: [] };
    return {
      ...(await sdk.createAgentSessionFromServices({
        services,
        sessionManager,
        sessionStartEvent,
        scopedModels: scope.scopedModels,
      })),
      services,
      diagnostics: [...services.diagnostics, ...scope.diagnostics],
    };
  };
  const runtime = await sdk.createAgentSessionRuntime(createRuntime, {
    cwd,
    agentDir,
    sessionManager,
  });
  try {
    sdk.initTheme(runtime.services.settingsManager.getTheme());
    console.log(
      `Pi credentials: ${authPath}${env.DEVBOX_SHARED_AUTH ? " (shared by Devbox instances)" : " (isolated)"}`,
    );
    await new sdk.InteractiveMode(runtime, {
      startupDiagnostics: runtime.diagnostics,
      modelFallbackMessage: runtime.modelFallbackMessage,
      autoTrustOnReloadCwd: sdk.hasTrustRequiringProjectResources(cwd)
        ? undefined
        : cwd,
    }).run();
  } finally {
    await runtime.dispose();
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    process.stdout.write(HELP);
    return;
  }
  if (options.offline) {
    process.env.PI_OFFLINE = "1";
    process.env.PI_SKIP_VERSION_CHECK = "1";
  }
  const sdk = await import("@earendil-works/pi-coding-agent");
  await launch(sdk, options);
}

if (
  process.argv[1] &&
  realpathSync(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  main().catch((error) => {
    console.error(`devbox-pi: ${error.message}`);
    process.exitCode = 1;
  });
}
