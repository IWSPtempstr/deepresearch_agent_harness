import { Fragment } from "react";
import { RunFlows } from "./RunFlows";

const REPO_BASE = "https://github.com/IWSPtempstr/deepresearch_agent_harness";

const NAV_ITEMS: Array<{ href: string; label: string }> = [
  { href: "#architecture", label: "架构" },
  { href: "#run-flows", label: "运行流程" },
  { href: "#reliability", label: "可靠性" },
  { href: "#evidence", label: "证据工程" },
  { href: "#engineering", label: "工程实践" },
];

const METRICS: Array<{ value: string; label: string }> = [
  { value: "5", label: "完整运行流程" },
  { value: "16", label: "Run 生命周期状态" },
  { value: "8", label: "Completion Contract 检查" },
  { value: "1", label: "不可变 Evidence Ledger" },
];

const FLOW_STEPS: Array<{ index: string; name: string; note: string; verify?: boolean }> = [
  { index: "01", name: "Context", note: "上下文构建" },
  { index: "02", name: "Plan", note: "规划任务" },
  { index: "03", name: "Execute", note: "检索与工具" },
  { index: "04", name: "Report", note: "组织证据" },
  { index: "05", name: "Verify", note: "完成门禁", verify: true },
];

const RELIABILITY_FEATURES: Array<{ icon: string; title: string; body: string; codeRef: string }> = [
  {
    icon: "01",
    title: "Lease 租约",
    body: "用带过期时间的租约约束执行权。worker 失联后，过期扫描可让另一个 worker 接管。",
    codeRef: "harness/runtime.py",
  },
  {
    icon: "02",
    title: "版本化 Checkpoint",
    body: "在阶段边界保存可校验状态，恢复时定位尚未提交的安全阶段，而不是盲目重跑完整任务。",
    codeRef: "harness/checkpoints.py",
  },
  {
    icon: "03",
    title: "幂等重放",
    body: "对事件、工具调用和证据去重，降低故障恢复和重复投递造成重复副作用的风险。",
    codeRef: "harness/runtime.py",
  },
  {
    icon: "04",
    title: "多维预算",
    body: "按 Token、工具调用、重试、重规划和墙钟时间限制任务成本；耗尽时明确终止，不伪装完成。",
    codeRef: "harness/budgets.py",
  },
];

const EVIDENCE_POINTS: Array<{ mark: string; title: string; body: string }> = [
  {
    mark: "A",
    title: "全量入账",
    body: "每条证据生成稳定 ID，记录来源、任务和工具调用上下文，支持从报告引用反查。",
  },
  {
    mark: "B",
    title: "按章节路由",
    body: "Evidence Card 将证据压缩并分配到报告章节，降低重复注入上下文的成本。",
  },
  {
    mark: "C",
    title: "覆盖门禁",
    body: "验证账本、卡片、路由、正文和附录的证据集合，避免证据在压缩或写作中被静默丢弃。",
  },
];

const STACK_ROWS: Array<{ index: string; name: string; detail: string }> = [
  { index: "01", name: "Workflow", detail: "deep_research · plan_execute_report" },
  { index: "02", name: "Persistence", detail: "PostgreSQL · Artifact Store · Neo4j" },
  { index: "03", name: "Execution", detail: "Redis Streams · Lease · Checkpoint" },
  { index: "04", name: "Quality", detail: "Completion Contract · pytest · CI" },
];

const REPO_LINKS: Array<{ tag: string; title: string; note: string; href: string }> = [
  {
    tag: "REPOSITORY",
    title: "查看代码与文档 ↗",
    note: "本地运行说明、源代码和版本历史",
    href: REPO_BASE,
  },
  {
    tag: "TESTING",
    title: "测试与 CI 记录 ↗",
    note: "后端测试 · 前端构建 · PostgreSQL CI",
    href: `${REPO_BASE}/actions`,
  },
  {
    tag: "ARCHITECTURE",
    title: "架构与设计文档 ↗",
    note: "Harness · Evidence · Recovery",
    href: `${REPO_BASE}/tree/main/docs`,
  },
];

export function PortfolioApp() {
  return (
    <div className="pf-root">
      <header className="pf-topbar">
        <a className="pf-brand" href="#top" aria-label="返回顶部">
          <span className="pf-brand-mark">D</span>
          <span>
            DEEPRESEARCH <b>HARNESS</b>
          </span>
        </a>
        <nav aria-label="主导航">
          {NAV_ITEMS.map((item) => (
            <a key={item.href} href={item.href}>
              {item.label}
            </a>
          ))}
          <a className="pf-nav-cta" href={REPO_BASE}>
            GitHub 仓库 ↗
          </a>
        </nav>
      </header>

      <main id="top">
        <section className="pf-shell pf-hero">
          <div className="pf-hero-copy">
            <p className="pf-eyebrow">
              <span className="pf-pulse" aria-hidden="true" />
              AI SYSTEMS · DURABLE EXECUTION · EVIDENCE FIRST
            </p>
            <h1>
              让研究型 Agent
              <br />
              <em>跑得完，也经得起验证。</em>
            </h1>
            <p className="pf-lede">
              DeepResearch Agent Harness 是一个面向长时、多步骤研究任务的可恢复、多智能体、证据驱动运行时。
              它把 Agent 放进有状态、可恢复、可验收的执行框架，而不是只生成一段看似合理的答案。
            </p>
            <div className="pf-hero-actions">
              <a className="pf-button pf-button-primary" href="#architecture">
                探索系统架构 <span aria-hidden="true">↓</span>
              </a>
              <a className="pf-button pf-button-quiet" href="#explore">
                查看仓库内容 <span aria-hidden="true">↗</span>
              </a>
            </div>
            <p className="pf-static-note">
              <span className="pf-dot" aria-hidden="true" />
              这是 GitHub 静态项目介绍页，不提供在线研究 API 或实时服务。
            </p>
          </div>
          <div className="pf-hero-visual" aria-hidden="true">
            <div className="pf-orbit pf-orbit-one" />
            <div className="pf-orbit pf-orbit-two" />
            <div className="pf-orbit-node pf-node-context">CONTEXT</div>
            <div className="pf-orbit-node pf-node-plan">PLAN</div>
            <div className="pf-orbit-node pf-node-execute">EXECUTE</div>
            <div className="pf-orbit-node pf-node-report">REPORT</div>
            <div className="pf-orbit-node pf-node-verify">VERIFY</div>
            <div className="pf-core-card">
              <span className="pf-core-symbol">◈</span>
              <strong>HARNESS</strong>
              <small>STATE · EVIDENCE · RECOVERY</small>
            </div>
            <div className="pf-visual-caption">
              CONTROL FLOW
              <br />
              <b>BY DESIGN</b>
            </div>
          </div>
        </section>

        <section className="pf-metrics-band">
          <div className="pf-shell pf-metrics-grid">
            {METRICS.map((metric) => (
              <div key={metric.label}>
                <span className="pf-metric">{metric.value}</span>
                <span className="pf-metric-label">{metric.label}</span>
              </div>
            ))}
          </div>
        </section>

        <section className="pf-section pf-shell" id="architecture">
          <div className="pf-section-heading">
            <p className="pf-eyebrow">01 / SYSTEM DESIGN</p>
            <h2>
              Agent 有自主性，
              <br />
              <em>控制流有边界。</em>
            </h2>
            <p>
              Harness 负责可靠运行，工作流负责组织研究。模型参与规划与生成，但不能自行宣布任务完成。
            </p>
          </div>
          <div className="pf-workflow-card">
            {FLOW_STEPS.map((step, index) => (
              <Fragment key={step.index}>
                {index > 0 && <i aria-hidden="true">→</i>}
                <div className={`pf-flow-step${step.verify ? " pf-flow-verify" : ""}`}>
                  <span>{step.index}</span>
                  <b>{step.name}</b>
                  <small>{step.note}</small>
                </div>
              </Fragment>
            ))}
            <div className="pf-flow-return">失败后按原因修稿、重规划，或诚实终止</div>
          </div>
          <div className="pf-workflow-caption">
            <span>固定阶段边界</span>
            <span>·</span>
            <span>可恢复状态</span>
            <span>·</span>
            <span>验证后完成</span>
          </div>
        </section>

        <RunFlows />

        <section className="pf-section pf-section-dark" id="reliability">
          <div className="pf-shell">
            <div className="pf-section-heading pf-section-heading-wide">
              <p className="pf-eyebrow">02 / DURABLE EXECUTION</p>
              <h2>
                长任务中断，
                <em>不必从头再来。</em>
              </h2>
              <p>
                平台模式把 API 和执行 worker 分开；Run 的状态、检查点和事件保存在持久层，队列只负责唤醒
                worker。
              </p>
            </div>
            <div className="pf-feature-grid">
              {RELIABILITY_FEATURES.map((feature) => (
                <article className="pf-feature-card" key={feature.title}>
                  <div className="pf-feature-icon">{feature.icon}</div>
                  <h3>{feature.title}</h3>
                  <p>{feature.body}</p>
                  <span className="pf-code-ref">{feature.codeRef}</span>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="pf-section pf-shell" id="evidence">
          <div className="pf-section-heading">
            <p className="pf-eyebrow">03 / EVIDENCE ENGINEERING</p>
            <h2>
              报告不是终点，
              <br />
              <em>可追溯才是。</em>
            </h2>
            <p>研究证据进入持久化账本，压缩卡只改变模型看到的表达，不丢失原始证据的回链。</p>
          </div>
          <div className="pf-evidence-layout">
            <div className="pf-evidence-visual" aria-hidden="true">
              <div className="pf-ledger-label">IMMUTABLE EVIDENCE LEDGER</div>
              <div className="pf-ledger-row">
                <span className="pf-ledger-id">ev_01</span>
                <span className="pf-ledger-line" />
                <span className="pf-ledger-tag">SOURCE A</span>
              </div>
              <div className="pf-ledger-row">
                <span className="pf-ledger-id">ev_02</span>
                <span className="pf-ledger-line" />
                <span className="pf-ledger-tag">SOURCE B</span>
              </div>
              <div className="pf-ledger-row">
                <span className="pf-ledger-id">ev_03</span>
                <span className="pf-ledger-line" />
                <span className="pf-ledger-tag">SOURCE C</span>
              </div>
              <div className="pf-ledger-arrow">↓ ROUTE BY SECTION</div>
              <div className="pf-card-row">
                <div className="pf-evidence-card-mini">
                  CARD 01 <small>claim + source ref</small>
                </div>
                <div className="pf-evidence-card-mini">
                  CARD 02 <small>claim + source ref</small>
                </div>
              </div>
              <div className="pf-coverage-check">
                <span>✓</span> LEDGER ↔ CARDS ↔ REPORT
              </div>
            </div>
            <div className="pf-evidence-copy">
              {EVIDENCE_POINTS.map((point) => (
                <article key={point.mark}>
                  <span className="pf-number">{point.mark}</span>
                  <div>
                    <h3>{point.title}</h3>
                    <p>{point.body}</p>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="pf-section pf-section-accent" id="engineering">
          <div className="pf-shell pf-engineering-grid">
            <div>
              <p className="pf-eyebrow">04 / ENGINEERING PRACTICE</p>
              <h2>
                为真实故障
                <br />
                而设计。
              </h2>
              <p>
                平台模式使用 PostgreSQL 持久化、Redis Streams 唤醒 worker、独立 worker 执行 Run；SSE
                以数据库事件为依据支持断线重放。
              </p>
            </div>
            <div className="pf-stack-list">
              {STACK_ROWS.map((row) => (
                <div key={row.index}>
                  <span>{row.index}</span>
                  <b>{row.name}</b>
                  <small>{row.detail}</small>
                </div>
              ))}
            </div>
          </div>
        </section>

        <section className="pf-section pf-shell pf-explore" id="explore">
          <div className="pf-section-heading">
            <p className="pf-eyebrow">05 / EXPLORE THE REPOSITORY</p>
            <h2>
              静态展示，
              <em>本地体验。</em>
            </h2>
            <p>
              此页面只介绍项目，不提供在线研究 API。需要体验交互式系统，请克隆仓库并按 README
              在本地配置运行；真实 LLM provider 需自行配置，默认不应产生未经确认的费用。
            </p>
          </div>
          <div className="pf-repo-links">
            {REPO_LINKS.map((link) => (
              <a className="pf-repo-card" key={link.tag} href={link.href}>
                <span>{link.tag}</span>
                <b>{link.title}</b>
                <small>{link.note}</small>
              </a>
            ))}
          </div>
        </section>
      </main>

      <footer className="pf-footer">
        <div className="pf-shell">
          <a className="pf-brand" href="#top">
            <span className="pf-brand-mark">D</span>
            <span>
              DEEPRESEARCH <b>HARNESS</b>
            </span>
          </a>
          <p>静态作品集 · 不提供在线 API · 以仓库代码和可复现证据为准</p>
          <a href="#top">回到顶部 ↑</a>
        </div>
      </footer>
    </div>
  );
}
