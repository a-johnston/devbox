// Synthetic OAuth provider: exercises Pi's real credential refresh transaction
// without contacting OpenAI or using any real credentials.
import assert from "node:assert/strict";
import { appendFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const [sdkPath, shared, countFile] = process.argv.slice(2);
const sdk = await import(pathToFileURL(sdkPath));
const runtime = await sdk.ModelRuntime.create({
  authPath: join(shared, "auth.json"),
});
const base = runtime.getProvider("openai-codex");
runtime.registerNativeProvider({
  ...base,
  id: "devbox-refresh-test",
  auth: {
    ...base.auth,
    oauth: {
      ...base.auth.oauth,
      async refresh(credential) {
        assert.equal(credential.refresh, "initial");
        appendFileSync(countFile, "refresh\n");
        await new Promise((resolve) => setTimeout(resolve, 300));
        return {
          type: "oauth",
          access: "fresh",
          refresh: "rotated",
          expires: Date.now() + 3_600_000,
        };
      },
    },
  },
});
const auth = await runtime.getAuth("devbox-refresh-test");
assert.equal(auth.auth.apiKey, "fresh");
