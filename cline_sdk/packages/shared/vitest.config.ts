import { defineConfig } from "vitest/config";

export default defineConfig({
	test: {
		environment: "node",
		include: ["src/**/*.test.ts"],
		// SQLite-backed测试通常会超过vitest的5秒默认值
		// 2核 Windows-latest 执行器。一个挂起保护，不是时间断言。
		testTimeout: 15_000,
		hookTimeout: 15_000,
		exclude: ["src/**/*.e2e.test.ts"],
	},
});
