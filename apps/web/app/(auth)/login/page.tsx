"use client";

import { useState } from "react";
import { ArrowRight, Database, Eye, EyeOff, LockKeyhole } from "lucide-react";
import { apiRequest, saveSession } from "@/lib/api";

export default function LoginPage() {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);
    setError("");
    const form = new FormData(event.currentTarget);
    const payload =
      mode === "login"
        ? { email: String(form.get("email") || ""), password: String(form.get("password") || "") }
        : {
            email: String(form.get("email") || ""),
            password: String(form.get("password") || ""),
            name: String(form.get("name") || ""),
            workspace_name: String(form.get("workspace_name") || "AI Product Workspace"),
          };
    try {
      const result = await apiRequest<{ access_token: string }>(
        mode === "login" ? "/auth/login" : "/auth/register",
        { method: "POST", body: JSON.stringify(payload) },
      );
      saveSession(result);
      window.location.href = "/";
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "登录失败，请重试");
    } finally {
      setLoading(false);
    }
  };
  return (
    <main className="login-shell">
      <div className="login-form-wrap">
        <div className="login-brand">
          <div className="brand-mark">
            <Database size={17} />
          </div>
          <div>
            <div className="brand-name">AI Product Workspace</div>
            <div className="brand-caption">让证据连接到决策</div>
          </div>
        </div>
        <form className="login-form" onSubmit={submit}>
          <div className="login-mode" role="tablist" aria-label="账号操作">
            <button
              type="button"
              className={mode === "login" ? "active" : ""}
              onClick={() => setMode("login")}
            >
              登录
            </button>
            <button
              type="button"
              className={mode === "register" ? "active" : ""}
              onClick={() => setMode("register")}
            >
              注册
            </button>
          </div>
          <h2>{mode === "login" ? "欢迎回来" : "创建 Owner 账号"}</h2>
          <p>
            {mode === "login"
              ? "登录以继续你的产品分析工作。"
              : "注册后自动创建你的工作空间。"}
          </p>
          {mode === "register" && (
            <>
              <div className="form-group">
                <label htmlFor="name">姓名</label>
                <input id="name" name="name" placeholder="你的姓名" required />
              </div>
              <div className="form-group">
                <label htmlFor="workspace_name">工作空间名称</label>
                <input
                  id="workspace_name"
                  name="workspace_name"
                  defaultValue="AI Product Workspace"
                  required
                />
              </div>
            </>
          )}
          {mode === "login" && (
            <p
              style={{
                margin: "0 0 14px",
                padding: "8px 10px",
                background: "var(--brand-soft)",
                border: "1px solid var(--line)",
                borderRadius: 8,
                color: "var(--muted)",
                fontSize: 12,
              }}
            >
              演示账号已预填（demo@apw.dev），直接点击「进入工作空间」即可体验。
            </p>
          )}
          <div className="form-group">
            <label htmlFor="email">邮箱</label>
            <input
              id="email"
              name="email"
              type="email"
              placeholder="name@company.com"
              defaultValue={mode === "login" ? "demo@apw.dev" : ""}
              key={mode}
              required
            />
          </div>
          <div className="form-group">
            <label htmlFor="password">密码</label>
            <div style={{ position: "relative" }}>
              <input
                id="password"
                name="password"
                type={showPassword ? "text" : "password"}
                style={{ paddingRight: 37 }}
                minLength={8}
                defaultValue={mode === "login" ? "Demo2026!apw" : ""}
                key={mode}
                required
              />
              <button
                type="button"
                aria-label={showPassword ? "隐藏密码" : "显示密码"}
                onClick={() => setShowPassword((value) => !value)}
                style={{
                  position: "absolute",
                  right: 10,
                  top: 10,
                  padding: 0,
                  border: 0,
                  background: "transparent",
                  color: "var(--faint)",
                }}
              >
                {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>
          <div className="form-foot">
            <label style={{ display: "flex", alignItems: "center", gap: 7 }}>
              <input type="checkbox" defaultChecked style={{ accentColor: "var(--brand)" }} />
              记住我
            </label>
          </div>
          {error && (
            <div className="form-error" role="alert">
              {error}
            </div>
          )}
          <button className="login-submit" type="submit" disabled={loading}>
            {loading ? (
              "正在处理…"
            ) : (
              <>
                <span>{mode === "login" ? "进入工作空间" : "创建工作空间"}</span>
                <ArrowRight size={16} style={{ verticalAlign: "middle", marginLeft: 6 }} />
              </>
            )}
          </button>
          <div className="form-hint">
            <LockKeyhole size={13} />
            <span>本地账号体系；AI 请求均由服务端转发，密钥不会下发至浏览器。</span>
          </div>
          <p style={{ textAlign: "center", marginTop: 24, color: "var(--muted)", fontSize: 12 }}>
            {mode === "login" ? "还没有账号？" : "已有账号？"}{" "}
            <button
              type="button"
              onClick={() => setMode(mode === "login" ? "register" : "login")}
              style={{ border: 0, background: "none", padding: 0, color: "var(--brand)", fontWeight: 500 }}
            >
              {mode === "login" ? "创建 Owner 账号" : "返回登录"}
            </button>
          </p>
        </form>
      </div>
    </main>
  );
}
