import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AutoMusic — Your next sound starts here",
  description: "프롬프트에서 보컬과 가사가 있는 노래까지. 나만의 음악 제작 공간, AutoMusic.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="ko"><body>{children}</body></html>;
}
