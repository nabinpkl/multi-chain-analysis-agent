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
  title: "JustGetDomain: Available Domain Names, Already Found For You",
  description:
    "Stop guessing if a domain is taken. JustGetDomain pre-scans every short domain combination and hands you only the ones that are actually available. 3, 4, 5-letter domains, already checked.",
  robots: "index, follow",
  alternates: {
    canonical: "https://justgetdomain.com/",
  },
  openGraph: {
    title: "JustGetDomain: Every Available Short Domain, Already Found",
    description:
      "We crawl every 3, 4, and 5-letter domain so you don't have to. Browse only what's available. No guessing, no taken results, no frustration.",
    type: "website",
    url: "https://justgetdomain.com/",
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
          storageKey="jgd-theme"
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
