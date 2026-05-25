import type { Metadata } from "next";
import { Inter, Instrument_Serif } from "next/font/google";
import { QueryProvider } from "@/components/query-provider";
import { SiteFooter } from "@/components/site-footer";
import { SiteNav } from "@/components/site-nav";
import { ThemeProvider } from "@/components/theme-provider";
import "./globals.css";

const inter = Inter({
  variable: "--font-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});

const instrumentSerif = Instrument_Serif({
  variable: "--font-serif",
  subsets: ["latin"],
  weight: "400",
  style: ["normal", "italic"],
});

export const metadata: Metadata = {
  title: "MultiChain Analysis Agent: LLM Analyst Over a Live Solana Graph",
  description:
    "An LLM agent that answers questions about wallet behavior on Solana mainnet, grounded in a real-time on-chain graph. Layered output verification, ablation switches, two-runtime parity, open and inspectable.",
  robots: "index, follow",
  alternates: {
    canonical: "https://chain.nabin.org/",
  },
  openGraph: {
    title: "MultiChain Analysis Agent: LLM Analyst Over a Live Solana Graph",
    description:
      "Ask questions about live on-chain wallet behavior. The agent uses a fixed set of typed primitives, every claim is structurally verified before it reaches the wire.",
    type: "website",
    url: "https://chain.nabin.org/",
  },
  icons: {
    icon: "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'%3E%3Crect width='512' height='512' rx='80' fill='%23050505'/%3E%3Ctext x='105' y='340' font-family='sans-serif' font-weight='700' font-size='280' fill='%2300ff41' letter-spacing='-15'%3E%26gt;_%3C/text%3E%3Ccircle cx='385' cy='375' r='20' fill='%2300ff41'/%3E%3C/svg%3E",
  }
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${instrumentSerif.variable} h-full antialiased`}
      suppressHydrationWarning
    >
      <body className="min-h-full flex flex-col" suppressHydrationWarning>
        <ThemeProvider
          attribute="class"
          defaultTheme="dark"
          enableSystem
          storageKey="mca-theme"
          disableTransitionOnChange
        >
          <QueryProvider>
            <SiteNav />
            <div className="flex-1">{children}</div>
            <SiteFooter />
          </QueryProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
