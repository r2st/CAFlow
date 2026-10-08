import { Link, useParams } from 'react-router-dom'

const ARTICLES = [
  {
    slug: 'email-outreach-automation-guide',
    title: 'Email Outreach Automation: The Complete Guide for 2026',
    excerpt: 'Learn how to set up automated email outreach campaigns that generate replies, not spam complaints.',
    date: 'October 3, 2026',
    readTime: '9 min read',
    content: `
<p>Email outreach remains one of the most effective channels for B2B growth. But sending emails manually doesn't scale, and blasting generic templates gets you marked as spam. The sweet spot is automation with personalization — and in 2026, AI makes that combination accessible to every startup.</p>

<h2>What Is Email Outreach Automation?</h2>
<p>Email outreach automation is the practice of using software to send targeted, personalized emails to prospects at scale, with automated follow-up sequences triggered by recipient behavior. Unlike email marketing (newsletters to subscribers), outreach targets people who haven't opted in — which means deliverability, relevance, and timing matter far more.</p>

<h2>The Core Components</h2>

<h3>1. Prospecting and List Building</h3>
<p>Your outreach is only as good as your list. Build targeted prospect lists based on industry, role, company size, geography, and intent signals. In India, LinkedIn is the primary source for B2B prospects. Tools like Apollo, Lusha, and Indian-specific databases like Zaubacorp help build lists with verified email addresses.</p>

<h3>2. Email Personalization</h3>
<p>Generic "Dear Sir/Madam" emails get deleted. Modern outreach uses AI to personalize at scale — referencing the recipient's company, recent news, mutual connections, or specific pain points. The best-performing cold emails feel like they were written by someone who did their homework.</p>

<h3>3. Sequence Design</h3>
<p>A single email rarely converts. Design 3-5 email sequences with increasing urgency or different angles. Space follow-ups 3-5 business days apart. Each follow-up should add value, not just "checking in." Common sequence:</p>
<ul>
<li><strong>Email 1</strong> — Introduce the problem and your solution</li>
<li><strong>Email 2</strong> — Share a relevant case study or result</li>
<li><strong>Email 3</strong> — Social proof (testimonial or metric)</li>
<li><strong>Email 4</strong> — Breakup email ("Last time reaching out")</li>
</ul>

<h3>4. Deliverability</h3>
<p>None of this matters if your emails land in spam. Key deliverability practices:</p>
<ul>
<li>Warm up new email accounts gradually (start with 10-20 sends/day)</li>
<li>Use a dedicated domain for outreach (not your main domain)</li>
<li>Set up SPF, DKIM, and DMARC authentication</li>
<li>Keep bounce rates below 3% by verifying emails before sending</li>
<li>Monitor your sender reputation with tools like Google Postmaster</li>
</ul>

<h2>Metrics That Matter</h2>
<ul>
<li><strong>Open rate</strong> — Target 40-60% for cold email (below 20% means subject lines or deliverability need work)</li>
<li><strong>Reply rate</strong> — 5-15% is good for cold outreach; above 15% is excellent</li>
<li><strong>Positive reply rate</strong> — Track interested responses separately from "please remove me" replies</li>
<li><strong>Meeting booked rate</strong> — The ultimate metric; aim for 2-5% of total sends</li>
</ul>

<h2>Common Mistakes</h2>
<ul>
<li><strong>Sending too many emails too fast</strong> — This destroys deliverability; ramp up gradually</li>
<li><strong>No unsubscribe mechanism</strong> — Required by law in most jurisdictions and expected by recipients</li>
<li><strong>Buying email lists</strong> — Low quality, high bounce rates, and often legally questionable</li>
<li><strong>Ignoring time zones</strong> — Send when recipients are at work; for India, 10am-12pm IST works best</li>
<li><strong>No A/B testing</strong> — Test subject lines, send times, and email length; small improvements compound</li>
</ul>

<h2>Getting Started</h2>
<p>You don't need a complex tech stack. Start with a verified prospect list, a dedicated sending domain, and a tool like DoAide Reach that handles personalization, sequencing, and deliverability in one platform. Focus on writing genuine, helpful emails to people who actually have the problem you solve — automation amplifies your approach, but it can't fix a bad one.</p>
`,
  },
  {
    slug: 'cold-email-best-practices-indian-startups',
    title: 'Cold Email Best Practices for Indian Startups',
    excerpt: 'What works (and what gets you blacklisted) when cold emailing in the Indian B2B market.',
    date: 'September 15, 2026',
    readTime: '7 min read',
    content: `
<p>Cold emailing in India is different from the US or Europe. The business culture, communication norms, and legal landscape all affect what works. Here's what Indian startups need to know to run cold email campaigns that generate leads without burning bridges.</p>

<h2>The Indian B2B Email Landscape</h2>
<p>India's B2B market is still warming up to cold email as a channel. Decision-makers receive fewer cold emails than their US counterparts, which means a well-crafted message stands out more. But it also means the bar for quality is high — a generic template feels even more impersonal against a low-volume inbox.</p>

<h2>What Works in India</h2>

<h3>1. Lead with Credibility</h3>
<p>Indian business culture values trust and relationships. Your first email should establish who you are and why the recipient should care. Mention mutual connections, shared institutions (IIT/IIM alumni networks are powerful), or recognizable clients. Name-dropping works differently here — it's not about showing off, it's about reducing perceived risk.</p>

<h3>2. Keep It Short and Specific</h3>
<p>Decision-makers in India are busy and often reading email on mobile. Keep your email under 150 words. Lead with the specific problem you solve, not a generic value proposition. "We help D2C brands reduce RTO rates by 30%" beats "We are a leading logistics technology company."</p>

<h3>3. Use WhatsApp as a Follow-Up Channel</h3>
<p>India is a WhatsApp-first market. After your initial email, a polite WhatsApp follow-up ("Hi [Name], just sent you an email about [topic] — would love 5 minutes of your time") can dramatically increase response rates. Always ask permission before adding someone to a WhatsApp group.</p>

<h3>4. Respect Indian Business Hours</h3>
<p>Send emails between 10 AM and 12 PM IST on weekdays. Avoid Monday mornings (inbox overload) and Friday afternoons (weekend mindset). Tuesday through Thursday morning is the sweet spot.</p>

<h3>5. Address the Price Question Early</h3>
<p>Indian buyers are price-conscious and will ask about cost early. If you have competitive pricing or a free tier, mention it in the first email. "Starting at ₹999/month" or "Free for teams under 5" removes a major objection upfront.</p>

<h2>What Gets You Blacklisted</h2>

<h3>Aggressive Follow-Up Cadence</h3>
<p>Sending follow-ups every day or every other day is a fast way to get reported as spam. Space follow-ups 4-5 business days apart. After 4 follow-ups with no response, stop. Persistence is good; pestering is not.</p>

<h3>Fake Personalization</h3>
<p>Indian professionals can spot a merge-tag mail merge instantly. "I was impressed by [Company Name]'s growth" without any specifics reads as lazy. If you can't find something genuinely specific about the recipient's company, at least reference their industry's challenges.</p>

<h3>Misleading Subject Lines</h3>
<p>"Re: Our conversation" when you've never spoken, or "Quick question" followed by a sales pitch, erodes trust immediately. Be straightforward about why you're writing.</p>

<h3>Ignoring the IT Act</h3>
<p>India's Information Technology Act and the upcoming Digital Personal Data Protection Act have provisions around unsolicited commercial communication. Include a clear unsubscribe option, honor removal requests within 24 hours, and never share or sell prospect data.</p>

<h2>Template That Works</h2>
<p>Here's a framework (not a copy-paste template) that performs well in Indian B2B:</p>
<ul>
<li><strong>Subject</strong> — Specific to their pain point (e.g., "Reducing [Company] RTO rates")</li>
<li><strong>Line 1</strong> — Why you're reaching out to THEM specifically</li>
<li><strong>Line 2-3</strong> — The specific problem you solve + one proof point</li>
<li><strong>CTA</strong> — Low-commitment ask ("Worth a 10-minute call this week?")</li>
<li><strong>Sign-off</strong> — Your name, title, company, and a link (no attachment)</li>
</ul>

<h2>Measuring Success</h2>
<p>Track these metrics weekly: open rate (target 45%+), reply rate (target 8%+), and meetings booked per 100 emails sent (target 3+). If your numbers are below these benchmarks after 200+ sends, revisit your targeting and messaging before scaling.</p>
`,
  },
  {
    slug: 'outreach-vs-ads-roi-comparison',
    title: 'Outreach vs Ads: Which Delivers Better ROI for Early-Stage Startups?',
    excerpt: 'A data-driven comparison of email outreach and paid advertising for startups with limited budgets.',
    date: 'September 1, 2026',
    readTime: '8 min read',
    content: `
<p>Every startup with limited budget faces the same question: should I spend on ads or invest in outreach? The answer depends on your market, your average deal size, and how quickly you need results. Here's a framework for deciding.</p>

<h2>The Cost Breakdown</h2>

<h3>Paid Advertising (Google/Meta)</h3>
<ul>
<li><strong>Google Ads (Search)</strong> — CPC for B2B keywords in India ranges from ₹30-200. A campaign targeting "CRM software India" might cost ₹100/click with a 2-3% landing page conversion rate, putting your cost per lead at ₹3,300-5,000.</li>
<li><strong>Meta Ads (Facebook/Instagram)</strong> — Lower CPC (₹10-50 for B2B), but lead quality is generally lower. Better for awareness than direct response. Cost per qualified lead typically ₹2,000-8,000.</li>
<li><strong>LinkedIn Ads</strong> — Highest quality B2B leads but expensive. CPC ranges from ₹200-600 in India. Cost per lead can easily reach ₹5,000-15,000.</li>
</ul>

<h3>Email Outreach</h3>
<ul>
<li><strong>Tools</strong> — ₹2,000-8,000/month for a sending tool with deliverability features</li>
<li><strong>Data</strong> — ₹0.5-5 per verified email address depending on source</li>
<li><strong>Time</strong> — 2-4 hours/week for list building, personalization, and response handling</li>
<li><strong>Cost per lead</strong> — Typically ₹500-2,000 for qualified leads when done well</li>
</ul>

<h2>When Outreach Wins</h2>

<h3>High-Value B2B Sales (Deal Size > ₹50,000)</h3>
<p>When your average contract value is high, the economics of personalized outreach are unbeatable. Spending 30 minutes researching and writing a custom email to a VP of Engineering is worth it when the deal is ₹5 lakhs. Ads can't match this level of targeting precision.</p>

<h3>Niche Markets</h3>
<p>If your total addressable market is 500 companies, running ads is inefficient — you'll pay for impressions from people who will never buy. Direct outreach to a curated list is more efficient and lets you tailor your message to each prospect's specific situation.</p>

<h3>Early Validation</h3>
<p>Before product-market fit, outreach gives you direct feedback. A prospect who responds "not interested, we already use X for this" teaches you more than a Google Analytics bounce. Outreach is a research channel as much as a sales channel.</p>

<h2>When Ads Win</h2>

<h3>High-Volume, Low-Touch Sales</h3>
<p>If you're selling a ₹999/month SaaS to SMBs, personalized outreach doesn't pencil out. The cost of acquiring a lead through email (even at ₹1,000) might exceed a month's revenue. Ads with a self-serve signup flow are more efficient for low-ACV products.</p>

<h3>Established Product-Market Fit</h3>
<p>Once you know your messaging, your ICP, and your conversion funnel, ads scale faster than outreach. You can spend ₹5 lakhs on Google Ads tomorrow and start getting leads; scaling outreach to the same volume takes weeks of list building and domain warming.</p>

<h3>Brand Awareness</h3>
<p>If your goal is awareness rather than immediate leads, display and social ads reach more people per rupee than email. Outreach is a 1:1 channel; ads are 1:many.</p>

<h2>The Hybrid Approach</h2>
<p>The best early-stage startups use both channels strategically:</p>
<ol>
<li><strong>Use outreach for your top 100 prospects</strong> — The companies where a deal would be transformative. Personalized, researched emails with thoughtful follow-ups.</li>
<li><strong>Use ads for the long tail</strong> — Retargeting visitors who came through outreach, running content promotion to build authority, and capturing inbound interest.</li>
<li><strong>Use outreach insights to inform ad copy</strong> — The objections and questions you hear in reply emails become the basis for ad creative and landing page copy.</li>
</ol>

<h2>ROI Comparison: Real Numbers</h2>
<p>Based on aggregated data from Indian B2B startups (deal size ₹1-10 lakhs/year):</p>
<ul>
<li><strong>Cold email outreach</strong> — 5-12x ROI over 6 months (₹1 spent on outreach generates ₹5-12 in revenue)</li>
<li><strong>Google Search ads</strong> — 3-8x ROI over 6 months (higher initial spend, faster results)</li>
<li><strong>LinkedIn ads</strong> — 2-5x ROI over 6 months (highest quality but most expensive per lead)</li>
<li><strong>Meta ads</strong> — 1-4x ROI for B2B over 6 months (better for B2C or very low-ticket B2B)</li>
</ul>

<h2>The Bottom Line</h2>
<p>For early-stage Indian startups with limited budgets and high-value offerings, email outreach delivers the best ROI per rupee spent. As you scale, layer in paid channels to complement your outreach engine. The mistake most startups make is choosing one channel exclusively — the real leverage comes from using outreach to learn what works, then amplifying those learnings through ads.</p>
`,
  },
]

function BlogIndex() {
  return (
    <div className="landing-root blog-page">
      <div className="blog-container">
        <Link to="/" className="blog-back">&larr; Back to DoAide Reach</Link>
        <h1 className="blog-page-title">Blog</h1>
        <p className="blog-page-subtitle">Email outreach strategies, tips, and insights for Indian startups.</p>
        <div className="blog-list">
          {ARTICLES.map((a) => (
            <Link key={a.slug} to={`/blog/${a.slug}`} className="blog-list-card">
              <div className="blog-list-meta">{a.date} &middot; {a.readTime}</div>
              <h2 className="blog-list-title">{a.title}</h2>
              <p className="blog-list-excerpt">{a.excerpt}</p>
            </Link>
          ))}
        </div>
      </div>
    </div>
  )
}

function BlogPost() {
  const { slug } = useParams()
  const article = ARTICLES.find((a) => a.slug === slug)

  if (!article) {
    return (
      <div className="landing-root blog-page">
        <div className="blog-container">
          <h1 className="blog-page-title">Article Not Found</h1>
          <Link to="/blog" className="blog-back">&larr; Back to Blog</Link>
        </div>
      </div>
    )
  }

  return (
    <div className="landing-root blog-page">
      <div className="blog-container">
        <Link to="/blog" className="blog-back">&larr; Back to Blog</Link>
        <div className="blog-list-meta">{article.date} &middot; {article.readTime}</div>
        <h1 className="blog-page-title">{article.title}</h1>
        <div className="blog-content" dangerouslySetInnerHTML={{ __html: article.content }} />
      </div>
    </div>
  )
}

export { BlogIndex, BlogPost, ARTICLES }
