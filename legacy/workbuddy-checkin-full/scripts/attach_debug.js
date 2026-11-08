// Read-only: enable the V8 inspector on an already-running WorkBuddy main process.
const pid = Number(process.argv[2] || 15432);
try {
  process._debugProcess(pid);
  console.log("ATTACHED", pid);
} catch (e) {
  console.error("ATTACH_FAIL", e && e.message);
  process.exit(1);
}
