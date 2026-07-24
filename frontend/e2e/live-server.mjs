import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const directory = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(directory, "../..");
const child = spawn(process.env.PYTHON ?? "python", ["scripts/live_frontend_test_server.py", "--port", "4176"], {
  cwd: root,
  env: { ...process.env, PYTHONPATH: path.join(root, "src") },
  stdio: "inherit",
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}
child.on("exit", (code) => process.exit(code ?? 1));
