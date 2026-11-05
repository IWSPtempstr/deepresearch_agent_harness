import { REPO_BASE, RUN_FLOWS, type RunFlow } from "./flows.data";

function FlowSection({ flow }: { flow: RunFlow }) {
  return (
    <section className="pf-section pf-flow-section" id={flow.id} aria-labelledby={`${flow.id}-title`}>
      <div className="pf-shell">
        <header className="pf-flow-head">
          <p className="pf-eyebrow">
            RUN FLOW · {flow.order}
          </p>
          <h2 id={`${flow.id}-title`}>
            {flow.name}
          </h2>
          <p className="pf-flow-tagline">{flow.tagline}</p>
        </header>

        <div className="pf-flow-meta">
          <div className="pf-meta-item">
            <span className="pf-meta-label">触发条件</span>
            <p>{flow.trigger}</p>
          </div>
          <div className="pf-meta-item">
            <span className="pf-meta-label">关键持久化</span>
            <p>{flow.persistence}</p>
          </div>
          <div className="pf-meta-item">
            <span className="pf-meta-label">成功出口</span>
            <p>{flow.successExit}</p>
          </div>
        </div>

        <ol className="pf-track">
          {flow.steps.map((step, index) => (
            <li className="pf-track-step" key={step.title}>
              <span className="pf-track-index">{String(index + 1).padStart(2, "0")}</span>
              <div className="pf-track-body">
                <b>{step.title}</b>
                <p>{step.detail}</p>
                {step.optionalNote ? <small className="pf-track-note">{step.optionalNote}</small> : null}
              </div>
            </li>
          ))}
        </ol>

        <div className="pf-branches">
          <span className="pf-branches-label">分支与恢复</span>
          <ul>
            {flow.branches.map((branch) => (
              <li className={`pf-branch pf-branch-${branch.tone}`} key={branch.label}>
                <b>{branch.label}</b>
                <p>{branch.detail}</p>
              </li>
            ))}
          </ul>
        </div>

        <footer className="pf-flow-refs">
          <span className="pf-branches-label">代码入口</span>
          <div className="pf-chip-row">
            {flow.codeRefs.map((ref) => (
              <a className="pf-chip" key={ref.path} href={`${REPO_BASE}/blob/main/${ref.path}`}>
                {ref.label} ↗
              </a>
            ))}
          </div>
        </footer>
      </div>
    </section>
  );
}

export function RunFlowsIndex({ flows }: { flows: RunFlow[] }) {
  return (
    <nav className="pf-flow-index pf-shell" aria-label="运行流程目录">
      {flows.map((flow) => (
        <a className="pf-flow-index-link" key={flow.id} href={`#${flow.id}`}>
          <span>{flow.order}</span>
          <b>{flow.name}</b>
          <small>{flow.tagline}</small>
        </a>
      ))}
    </nav>
  );
}

export function RunFlows() {
  return (
    <>
      <section className="pf-section pf-shell" id="run-flows">
        <div className="pf-section-heading">
          <p className="pf-eyebrow">RUN FLOWS</p>
          <h2>
            五条完整运行流程，
            <br />
            <em>从一条消息到验证完成。</em>
          </h2>
          <p>
            每条流程独立成章：触发条件、逐步执行、关键持久化、成功出口、失败与恢复分支，以及对应源码入口。
            所有描述均与仓库实现一一对应。
          </p>
        </div>
        <RunFlowsIndex flows={RUN_FLOWS} />
      </section>
      {RUN_FLOWS.map((flow) => (
        <FlowSection flow={flow} key={flow.id} />
      ))}
    </>
  );
}
