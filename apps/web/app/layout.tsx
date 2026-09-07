import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Product Workspace — 数据驱动的产品决策工作台",
  description: "确定性计算 + 人工裁决 + 证据链：从数据上传到 PRD 交付的可追溯产品决策工作流。",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
