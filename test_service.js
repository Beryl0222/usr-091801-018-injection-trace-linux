"use strict";

const { spawnSync } = require("node:child_process");

// 依次运行服务契约测试与领域规则测试，任一失败即整体失败
const suites = [
  ["-m", "unittest", "-v", "service_contract"],
  ["-m", "unittest", "-v", "traceability.test_traceability"],
];

for (const args of suites) {
  const result = spawnSync("python3", args, { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) {
    process.exit(result.status ?? 1);
  }
}

// 跨门店复诊追查验收场景
const scenario = spawnSync(
  "python3",
  ["-m", "traceability.scenario"],
  { stdio: "inherit" },
);
if (scenario.error) {
  console.error(scenario.error.message);
  process.exit(1);
}
process.exit(scenario.status ?? 1);
