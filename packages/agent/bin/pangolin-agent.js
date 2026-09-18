#!/usr/bin/env node
const [major, minor] = process.versions.node.split('.').map(Number);
if (major < 22 || (major === 22 && minor < 13)) {
  console.error('Pangolin Agent 需要 Node.js 22.13+，请使用 curl 安装脚本准备运行环境。');
  process.exitCode = 1;
} else {
  try { const { main } = await import('../src/cli.js'); await main(); }
  catch (error) { console.error('Pangolin: ' + error.message); process.exitCode = 1; }
}
