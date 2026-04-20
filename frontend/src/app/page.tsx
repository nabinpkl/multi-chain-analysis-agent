import { LinkedInBlock } from "@/components/home/linkedin-block";

const jsonLd = {
  "@context": "https://schema.org",
  "@type": "WebApplication",
  name: "ChainAnalysis",
  url: "https://chain.nabin.org",
  description:
    "ChainAnalysis is a tool for analyzing blockchain data.",
  applicationCategory: "DeveloperApplication",
  operatingSystem: "Web",
  offers: {
    "@type": "Offer",
    price: "0",
    priceCurrency: "USD",
    availability: "https://schema.org/PreOrder",
  },
};

export default function Home() {
  return (<div />)
  // (
  //   <div>
  //     <script
  //       type="application/ld+json"
  //       dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd) }}
  //     />
  //     <div className="overflow-x-clip bg-jgd-bg text-jgd-text font-sans font-medium leading-[1.7]">

  //       {/* LinkedIn / built by */}
  //       <LinkedInBlock />
  //     </div>
  //   <div/>
  // );
}
