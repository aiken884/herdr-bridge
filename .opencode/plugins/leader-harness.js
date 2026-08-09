// opencode tool.execute.before plugin：把判斷委派給獨立 Python 子行程
// （leader_harness_check.py），維持跟其他品牌一致的單一判斷邏輯來源。
//
// STABILITY: 這個 plugin 目前只用 tool.execute.before（穩定 API）。
// experimental.session.compacting（context 壓縮後重新注入）標記為 experimental，
// 官方保留隨時變更/移除的權利，尚未串接——升級 opencode 版本時需要重新確認這個
// API 是否還存在，才能考慮串接。
import { spawnSync } from "node:child_process"
import { join } from "node:path"

export const LeaderHarnessPlugin = async () => {
  return {
    "tool.execute.before": async (input, output) => {
      const writeTools = ["write", "edit"]
      if (!writeTools.includes(input.tool)) return

      const filePath = output.args?.filePath ?? output.args?.file_path ?? ""
      const scriptPath = join(
        process.env.HERDR_BRIDGE_ROOT || process.cwd(),
        ".opencode", "plugins", "leader_harness_check.py"
      )
      const payload = JSON.stringify({ tool: input.tool, file_path: filePath })

      const result = spawnSync("python3", [scriptPath], {
        input: payload,
        encoding: "utf-8",
        env: { ...process.env, HERDR_BRIDGE_ROOT: process.env.HERDR_BRIDGE_ROOT || process.cwd() },
      })

      if (result.status !== 0 || !result.stdout) {
        throw new Error(
          "[HERDR-LEADER-HARNESS BLOCKED] Tool: " + input.tool + " | " +
          "Reason: Leader Harness 判斷子行程執行失敗，fail-closed 拒絕。 " +
          "Do not retry this tool call. Inform the user of the restriction."
        )
      }

      const decision = JSON.parse(result.stdout)
      if (decision.decision === "deny") {
        throw new Error(
          "[HERDR-LEADER-HARNESS BLOCKED] Tool: " + input.tool + " | " +
          "Reason: " + decision.reason + " | " +
          "This operation is restricted by the Leader Harness policy. " +
          "Do not retry this tool call. Inform the user of the restriction."
        )
      }
    },
  }
}
