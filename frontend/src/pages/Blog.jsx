import { useEffect } from 'react'
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
    faqs: [
      { q: 'What is email outreach automation?', a: 'Email outreach automation is using software to send targeted, personalized emails to prospects at scale with automated follow-up sequences triggered by recipient behavior such as opens, clicks, or no response.' },
      { q: 'How many follow-up emails should I send?', a: 'A typical cold email sequence includes 3 to 5 follow-up emails spaced 3 to 5 business days apart. Each follow-up should add new value rather than simply checking in.' },
      { q: 'What is a good open rate for cold emails?', a: 'A good open rate for cold outreach emails is 40 to 60 percent. If your open rate is below 20 percent, your subject lines or email deliverability need improvement.' },
    ],
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
    faqs: [
      { q: 'Is cold emailing legal in India?', a: 'Cold emailing is legal in India but subject to regulations under the Information Technology Act and the Digital Personal Data Protection Act. You must include an unsubscribe option and honor removal requests within 24 hours.' },
      { q: 'What is the best time to send cold emails in India?', a: 'The best time to send cold emails in India is between 10 AM and 12 PM IST on weekdays, particularly Tuesday through Thursday. Avoid Monday mornings and Friday afternoons.' },
      { q: 'How long should a cold email be for Indian prospects?', a: 'Cold emails for the Indian B2B market should be under 150 words. Lead with the specific problem you solve and include a low-commitment call to action.' },
    ],
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
    faqs: [
      { q: 'Is email outreach or paid ads better for Indian startups?', a: 'For early-stage Indian startups with high-value B2B offerings, email outreach typically delivers 5 to 12x ROI compared to 3 to 8x for Google Ads and 2 to 5x for LinkedIn Ads over six months.' },
      { q: 'What is the cost per lead for cold email outreach in India?', a: 'Cold email outreach in India typically costs between 500 and 2000 rupees per qualified lead when done well, which is significantly lower than Google Ads or LinkedIn Ads.' },
      { q: 'Should startups use both outreach and ads?', a: 'Yes. The best approach is a hybrid strategy — use personalized outreach for your top prospects and paid ads for retargeting and long-tail demand generation.' },
    ],
  },
  {
    slug: 'b2b-lead-generation-strategies-india',
    title: 'B2B Lead Generation Strategies That Work in India in 2026',
    excerpt: 'Proven lead generation tactics for Indian SaaS companies, agencies, and service providers targeting domestic and global clients.',
    date: 'October 8, 2026',
    readTime: '10 min read',
    content: `
<p>India's B2B market crossed $10 billion in SaaS revenue in 2025, and the pipeline that feeds it runs on lead generation. But what works in the US doesn't always translate. Indian buyers research differently, trust differently, and buy differently. Here are the lead generation strategies that actually convert in the Indian market right now.</p>

<h2>Why B2B Lead Generation in India Is Different</h2>
<p>Three forces shape Indian B2B buying behavior. First, relationships still outweigh brand recognition — a warm introduction from a mutual connection closes faster than any inbound funnel. Second, price sensitivity is higher; Indian buyers compare three to five vendors before deciding and negotiate harder on annual contracts. Third, the decision-making unit is larger in Indian companies; even mid-market deals often involve a founder, a finance head, and a department lead.</p>
<p>Understanding these dynamics is the difference between a pipeline that leaks and one that converts.</p>

<h2>Strategy 1: LinkedIn as a Lead Engine</h2>
<h3>Optimize Your Company Page and Personal Profiles</h3>
<p>LinkedIn is the undisputed B2B platform in India, with over 130 million Indian members. Yet most companies treat their page as a dead resume. Turn it into a lead magnet: post case studies with specific Indian client metrics, share founder-led content about industry problems, and engage genuinely in comments on posts by your ideal customers.</p>

<h3>Content That Generates Inbound Leads</h3>
<p>Document-style posts outperform promotional ones. "How we helped a Bangalore fintech reduce churn by 22% in 90 days" generates more leads than "Proud to announce our Series A." Indian decision-makers engage with tactical content that teaches them something applicable to their business today.</p>

<h3>LinkedIn Sales Navigator for Targeted Outreach</h3>
<p>Sales Navigator lets you filter by geography (city-level in India), company headcount, industry, and seniority. Build saved searches for your ideal customer profile and use the lead recommendations to find prospects you would have missed. Combine this with email outreach — find the prospect on LinkedIn, then reach out via email with a personalized reference to their recent LinkedIn activity.</p>

<h2>Strategy 2: Cold Email Outreach with Indian Market Targeting</h2>
<p>Cold email remains the highest-ROI outbound channel for Indian B2B companies. The key differences for the Indian market:</p>
<ul>
<li><strong>Use .co.in or .in domains for sending</strong> — Indian recipients trust Indian domain extensions more than generic .com addresses from unknown companies</li>
<li><strong>Reference Indian business context</strong> — mention GST compliance, SEBI regulations, RBI guidelines, or DPDP Act depending on the industry</li>
<li><strong>Price in INR</strong> — always quote pricing in rupees, never dollars, even if you serve global clients</li>
<li><strong>Follow up on WhatsApp</strong> — after the second email, a brief WhatsApp message ("Hi [Name], sent you an email about [topic]") increases reply rates by 30-40% in Indian outreach campaigns</li>
</ul>
<p>A tool like DoAide Reach handles the sequencing, personalization, and deliverability so you can focus on writing messages that resonate with Indian buyers.</p>

<h2>Strategy 3: Webinars and Virtual Events</h2>
<p>Indian B2B buyers love learning before buying. Webinars that teach a specific skill or framework — not product demos disguised as education — generate high-quality leads. The format that works best in India:</p>
<ul>
<li><strong>Duration</strong> — 30-40 minutes, not 60. Indian professionals drop off sharply after 35 minutes</li>
<li><strong>Timing</strong> — Thursday 3 PM IST consistently outperforms other slots for B2B webinars in India</li>
<li><strong>Co-host with a known name</strong> — Partner with an industry leader, a well-known CXO, or an IIT/IIM professor. The co-host's network doubles your registrations</li>
<li><strong>Gate the recording, not the live session</strong> — Let anyone attend live but require registration for the recording. This captures leads who couldn't attend and rewards those who showed up</li>
</ul>

<h2>Strategy 4: SEO and Content Marketing for Indian Searches</h2>
<p>Indian B2B buyers search in English but with India-specific modifiers. "CRM software India," "HRMS for Indian companies," and "GST billing software" get more qualified traffic than their generic equivalents. Target these long-tail keywords with in-depth blog posts and landing pages.</p>
<p>Create comparison content — "Top 10 CRM tools for Indian startups" or "Zoho vs Freshworks for Indian SMBs" — because Indian buyers heavily research before committing. These pages rank well and attract prospects who are already evaluating solutions.</p>

<h2>Strategy 5: Partner and Referral Programs</h2>
<p>In a relationship-driven market, referrals are gold. Structure your referral program around what motivates Indian partners:</p>
<ul>
<li><strong>Revenue share over one-time bounties</strong> — Indian partners prefer ongoing income; offer 15-20% recurring commission</li>
<li><strong>CA and CS firm partnerships</strong> — For finance, compliance, and legal SaaS, chartered accountant and company secretary firms are the most powerful referral channel in India. They advise thousands of SMBs and their recommendation carries implicit trust</li>
<li><strong>Startup ecosystem partnerships</strong> — Partner with accelerators (T-Hub, NASSCOM 10K Startups, Startup India), coworking spaces (WeWork, 91springboard), and industry associations (NASSCOM, CII, FICCI) to reach concentrated groups of potential buyers</li>
</ul>

<h2>Strategy 6: WhatsApp Business for Lead Nurturing</h2>
<p>With over 500 million WhatsApp users in India, ignoring this channel means ignoring where your prospects actually communicate. Use WhatsApp Business API for:</p>
<ul>
<li><strong>Lead qualification</strong> — automated chatbot flows that qualify inbound leads before routing to sales</li>
<li><strong>Event reminders</strong> — webinar and demo reminders via WhatsApp get 3x the open rate of email reminders in India</li>
<li><strong>Content delivery</strong> — share bite-sized insights and case study summaries directly; Indian professionals consume more content on WhatsApp than on email newsletters</li>
</ul>

<h2>Measuring What Works</h2>
<p>Track these metrics monthly to evaluate your lead generation mix:</p>
<ul>
<li><strong>Cost per qualified lead (CPQL)</strong> — by channel; outreach should be under ₹2,000, paid ads under ₹5,000</li>
<li><strong>Lead-to-meeting conversion</strong> — target 15-25% for outbound leads, 25-40% for inbound</li>
<li><strong>Sales cycle length</strong> — Indian B2B deals take 45-90 days on average; if yours exceeds 90 days, your lead qualification criteria may be too loose</li>
<li><strong>Channel attribution</strong> — track first-touch and last-touch attribution separately; the channel that starts conversations is often different from the one that closes them</li>
</ul>

<h2>Getting Started</h2>
<p>Don't try all six strategies at once. Start with one outbound channel (cold email or LinkedIn outreach) and one inbound channel (SEO content or webinars). Measure for 60 days, then double down on what works and add a third channel. The Indian B2B market rewards consistency and patience — the company that shows up reliably in a prospect's inbox and feed for three months wins the deal when the budget opens up.</p>
`,
    faqs: [
      { q: 'What is the best B2B lead generation channel in India?', a: 'Cold email outreach and LinkedIn are the most effective B2B lead generation channels in India. Cold email delivers the lowest cost per qualified lead at 500 to 2000 rupees, while LinkedIn provides the richest targeting for Indian decision-makers with over 130 million members.' },
      { q: 'How much does B2B lead generation cost in India?', a: 'B2B lead generation costs in India vary by channel. Cold email outreach costs 500 to 2000 rupees per qualified lead, Google Ads costs 3300 to 5000 rupees per lead, and LinkedIn Ads can reach 5000 to 15000 rupees per lead.' },
      { q: 'How long is the average B2B sales cycle in India?', a: 'The average B2B sales cycle in India is 45 to 90 days. Indian buyers typically compare 3 to 5 vendors before deciding and involve multiple stakeholders including founders, finance heads, and department leads in the decision.' },
      { q: 'Should Indian startups use WhatsApp for B2B lead generation?', a: 'Yes. With over 500 million WhatsApp users in India, WhatsApp Business is a powerful lead nurturing channel. WhatsApp follow-ups after cold emails increase reply rates by 30 to 40 percent, and WhatsApp reminders get 3x the open rate of email.' },
    ],
  },
  {
    slug: 'email-deliverability-guide-indian-domains',
    title: 'Email Deliverability Guide: How to Keep Your Emails Out of Spam in India',
    excerpt: 'SPF, DKIM, DMARC, and domain warm-up explained for Indian businesses sending outreach and transactional emails.',
    date: 'October 6, 2026',
    readTime: '10 min read',
    content: `
<p>You wrote the perfect cold email. Your subject line is compelling, your copy is personalized, and your call-to-action is clear. But none of it matters if your email lands in the spam folder. In India, where Gmail dominates business email and spam filters are increasingly aggressive, deliverability is the foundation that every outreach campaign is built on.</p>

<h2>What Is Email Deliverability?</h2>
<p>Email deliverability is the percentage of your emails that reach the recipient's inbox rather than their spam folder, promotions tab, or being bounced back entirely. It depends on three factors: your sender reputation, your email authentication setup, and your sending behavior. Get all three right and your inbox placement rate stays above 90%. Get any one wrong and you might as well be shouting into a void.</p>

<h2>Step 1: Set Up Email Authentication (SPF, DKIM, DMARC)</h2>

<h3>SPF (Sender Policy Framework)</h3>
<p>SPF tells receiving email servers which IP addresses are allowed to send email on behalf of your domain. Without SPF, anyone can spoof your domain to send spam, and your legitimate emails get punished for it.</p>
<p>To set up SPF, add a TXT record to your domain's DNS. For example, if you send through Google Workspace and a cold email tool:</p>
<p><code>v=spf1 include:_spf.google.com include:your-email-tool.com ~all</code></p>
<p>Common mistakes Indian businesses make with SPF:</p>
<ul>
<li><strong>Too many DNS lookups</strong> — SPF allows a maximum of 10 DNS lookups. Indian companies using multiple tools (Google Workspace + Zoho + a cold email tool + a transactional email service) often exceed this limit, which silently breaks SPF</li>
<li><strong>Using +all instead of ~all or -all</strong> — +all tells servers to accept email from ANY sender claiming your domain, which defeats the entire purpose of SPF</li>
<li><strong>Forgetting subdomains</strong> — If you send outreach from outreach.yourdomain.in, that subdomain needs its own SPF record</li>
</ul>

<h3>DKIM (DomainKeys Identified Mail)</h3>
<p>DKIM adds a cryptographic signature to every email you send. The receiving server verifies this signature against a public key in your DNS records, confirming the email was actually sent by you and wasn't modified in transit.</p>
<p>DKIM is configured in your email provider's admin panel and requires adding a CNAME or TXT record to your DNS. Most Indian domain registrars (GoDaddy India, BigRock, HostGator India, Hostinger) support DKIM records — the process takes five minutes but protects every email you send.</p>

<h3>DMARC (Domain-based Message Authentication, Reporting, and Conformance)</h3>
<p>DMARC builds on SPF and DKIM by telling receiving servers what to do when an email fails authentication. It also sends you reports about who is using your domain to send email — legitimate or not.</p>
<p>Start with a monitoring-only policy:</p>
<p><code>v=DMARC1; p=none; rua=mailto:dmarc-reports@yourdomain.in</code></p>
<p>After two weeks of monitoring (and fixing any issues the reports reveal), tighten to:</p>
<p><code>v=DMARC1; p=quarantine; rua=mailto:dmarc-reports@yourdomain.in</code></p>
<p>Eventually move to <code>p=reject</code> for maximum protection. This gradual approach prevents you from accidentally blocking your own legitimate emails.</p>

<h2>Step 2: Domain Warm-Up for New Sending Domains</h2>
<p>If you are starting outreach with a new domain or a new email account, sending 500 emails on day one is a guaranteed trip to the spam folder. Email providers like Gmail and Outlook track sending patterns, and a sudden spike from an unknown sender triggers spam filters immediately.</p>

<h3>The Warm-Up Schedule</h3>
<ul>
<li><strong>Week 1</strong> — Send 10-15 emails per day to engaged contacts (people who will open and reply)</li>
<li><strong>Week 2</strong> — Increase to 25-30 emails per day</li>
<li><strong>Week 3</strong> — Increase to 50-60 emails per day</li>
<li><strong>Week 4</strong> — Increase to 80-100 emails per day</li>
<li><strong>Week 5 onwards</strong> — Gradually scale to your target volume, never increasing by more than 20% per week</li>
</ul>

<h3>Warm-Up Best Practices for Indian Domains</h3>
<ul>
<li><strong>Use a dedicated subdomain</strong> — Send outreach from outreach.yourdomain.in or mail.yourdomain.in, not your primary domain. If the outreach domain gets flagged, your main business email stays clean</li>
<li><strong>Start with your network</strong> — Send the first 50-100 warm-up emails to colleagues, friends, and existing clients who will open and reply. These positive engagement signals tell Gmail your domain is legitimate</li>
<li><strong>Mix outreach with transactional</strong> — During warm-up, don't send ONLY cold emails. Mix in newsletter-style content, meeting confirmations, and genuine correspondence to create a natural-looking sending pattern</li>
</ul>

<h2>Step 3: List Hygiene and Verification</h2>
<p>In the Indian market, email data quality is a persistent challenge. Business email addresses change frequently as professionals move between companies, and many Indian SMBs use personal Gmail addresses for business communication, which are harder to verify.</p>

<h3>Before Every Campaign</h3>
<ul>
<li><strong>Verify every email address</strong> — Use an email verification service to check each address before sending. Remove hard bounces, role-based addresses (info@, sales@), and disposable domains</li>
<li><strong>Target a bounce rate under 2%</strong> — If your bounce rate exceeds 3%, stop the campaign immediately and clean your list. High bounce rates signal to ISPs that you are sending to unverified addresses, which tanks your sender reputation</li>
<li><strong>Remove non-engagers after 3 campaigns</strong> — If a contact hasn't opened or clicked any of your last 3 campaigns, remove them. Continuing to send to unengaged contacts drags down your overall engagement metrics</li>
</ul>

<h2>Step 4: Sending Practices That Protect Deliverability</h2>

<h3>Timing and Volume</h3>
<ul>
<li><strong>Send during Indian business hours</strong> — 10 AM to 12 PM IST for the first email in a sequence. Follow-ups can go out at 2 PM to 4 PM IST</li>
<li><strong>Spread sends across the day</strong> — Don't send 100 emails at 10:00 AM sharp. Distribute them across a 2-3 hour window to mimic natural sending behavior</li>
<li><strong>Respect daily limits</strong> — Google Workspace allows 2,000 emails per day; keep cold outreach well below this. A safe maximum is 100-150 cold emails per day per account for warmed-up domains</li>
</ul>

<h3>Content That Passes Spam Filters</h3>
<ul>
<li><strong>Avoid spam trigger words</strong> — "Free," "guaranteed," "act now," "limited time offer" in subject lines increase spam classification risk. Write subject lines that read like a human colleague, not a marketer</li>
<li><strong>Keep HTML minimal</strong> — Plain text emails or lightly formatted HTML outperform heavily designed templates in cold outreach. Avoid images in the first email — they add tracking pixels and increase the spam score</li>
<li><strong>Include an unsubscribe link</strong> — Required by Indian IT Act provisions and by Gmail's sender guidelines since February 2024. DoAide Reach adds this automatically to every campaign</li>
<li><strong>Use a real reply-to address</strong> — No-reply addresses hurt deliverability and signal that you don't care about the recipient's response</li>
</ul>

<h2>Step 5: Monitor Your Sender Reputation</h2>
<p>Your sender reputation is a score that ISPs assign to your domain and IP address based on your sending behavior. Think of it like a credit score for email.</p>
<ul>
<li><strong>Google Postmaster Tools</strong> — Free, essential for anyone sending to Gmail users (which is most of corporate India). It shows your domain reputation, spam rate, and authentication success rate</li>
<li><strong>Check blacklists regularly</strong> — Use MXToolbox or MultiRBL to check if your domain or IP has been blacklisted. If you find yourself on a blacklist, stop all outreach immediately, identify the cause, and submit a delisting request</li>
<li><strong>Track inbox placement</strong> — Your open rate is a proxy for inbox placement. If open rates drop suddenly (from 45% to 15% overnight), it almost certainly means your emails moved from inbox to spam</li>
</ul>

<h2>Common Deliverability Mistakes in the Indian Market</h2>
<ul>
<li><strong>Using your primary .com domain for outreach</strong> — If your outreach domain gets flagged, your transactional emails (invoices, password resets, client updates) also suffer. Always use a separate domain</li>
<li><strong>Ignoring the Promotions tab</strong> — Gmail sorts many legitimate emails into the Promotions tab instead of the Primary inbox. Reducing HTML, removing marketing-style formatting, and writing conversational subject lines help your emails land in Primary</li>
<li><strong>Not monitoring shared IP reputation</strong> — If you use a shared sending IP (common with budget email tools), other senders on the same IP can tank your deliverability. Consider a tool like DoAide Reach that manages IP reputation and provides dedicated sending infrastructure</li>
</ul>

<h2>Frequently Asked Questions</h2>
<p><strong>How long does domain warm-up take?</strong> — Plan for 4-6 weeks before reaching full sending volume. Rushing the warm-up is the most common reason Indian startups get flagged as spam within their first month of outreach.</p>
<p><strong>Can I send cold emails from a free Gmail account?</strong> — Technically yes, but practically no. Free Gmail accounts have a 500 email daily limit, lack custom DKIM/DMARC, and look unprofessional. Use Google Workspace or a dedicated sending infrastructure.</p>
<p><strong>What is a good inbox placement rate?</strong> — Target 90% or above. Below 80% means your authentication or reputation needs attention. Below 60% means your emails are being actively filtered as spam.</p>
`,
    faqs: [
      { q: 'What are SPF, DKIM, and DMARC and why do they matter for Indian businesses?', a: 'SPF, DKIM, and DMARC are email authentication protocols. SPF specifies which IP addresses can send email for your domain. DKIM adds a cryptographic signature to verify email authenticity. DMARC tells receiving servers how to handle failed authentication. All three are essential to prevent your emails from landing in spam.' },
      { q: 'How long does email domain warm-up take?', a: 'Email domain warm-up takes 4 to 6 weeks. Start with 10 to 15 emails per day in week one and gradually increase by 20 percent per week until you reach your target volume of 100 to 150 cold emails per day.' },
      { q: 'What is a good email deliverability rate?', a: 'A good inbox placement rate is 90 percent or above. Below 80 percent means your authentication or sender reputation needs attention. Below 60 percent means your emails are being actively filtered as spam.' },
      { q: 'Can I send cold emails from Gmail in India?', a: 'You can but should not. Free Gmail accounts have a 500 email daily limit, lack custom DKIM and DMARC authentication, and look unprofessional. Use Google Workspace or a dedicated sending tool like DoAide Reach for business outreach.' },
    ],
  },
  {
    slug: 'build-cold-email-list-india-without-buying-data',
    title: 'How to Build a Cold Email List in India Without Buying Data',
    excerpt: 'Ethical, effective ways to build high-quality prospect lists for Indian B2B outreach without purchasing third-party databases.',
    date: 'October 5, 2026',
    readTime: '9 min read',
    content: `
<p>Buying email lists is the fastest way to destroy your sender reputation. Those lists are full of outdated addresses, spam traps, and people who never consented to hearing from you. For Indian businesses, the risk is even higher — the Digital Personal Data Protection Act (DPDP Act, 2023) imposes strict consent requirements, and the penalties for violations are severe. Here is how to build a high-quality prospect list from scratch, ethically and effectively.</p>

<h2>Why You Should Never Buy Email Lists in India</h2>
<p>Before diving into alternatives, understand what happens when you buy a list:</p>
<ul>
<li><strong>Bounce rates explode</strong> — Purchased lists typically have 15-30% invalid addresses. A bounce rate above 3% damages your sender reputation with Gmail and Outlook, and above 5% can get your domain blacklisted</li>
<li><strong>Spam trap hits</strong> — Email providers seed purchased databases with trap addresses. Sending to even one spam trap can land your entire domain on a blacklist</li>
<li><strong>Legal exposure</strong> — Under the DPDP Act, sending unsolicited emails to individuals without a legitimate business interest or reasonable context violates data processing norms. The penalty for significant non-compliance can reach ₹250 crore</li>
<li><strong>Zero engagement</strong> — People who didn't opt in don't open, don't reply, and often report you as spam. This trains ISPs to filter all your future emails</li>
</ul>

<h2>Method 1: LinkedIn Prospecting</h2>
<p>LinkedIn is the richest source of B2B prospect data in India, with over 130 million Indian members. Here is how to turn LinkedIn into a lead list systematically:</p>

<h3>Define Your Ideal Customer Profile (ICP)</h3>
<p>Before you search, know exactly who you are looking for. Be specific:</p>
<ul>
<li><strong>Industry</strong> — "Fintech companies" not "technology companies"</li>
<li><strong>Company size</strong> — "50-200 employees" not "mid-market"</li>
<li><strong>Decision-maker title</strong> — "VP of Marketing" or "Head of Growth" not "management"</li>
<li><strong>Geography</strong> — "Bangalore and Mumbai" not "India" (unless your product truly serves all of India equally)</li>
</ul>

<h3>LinkedIn Sales Navigator Search</h3>
<p>Use Sales Navigator's advanced filters to build targeted prospect lists. Key filters for the Indian market:</p>
<ul>
<li><strong>Company headquarters</strong> — Filter by Indian cities where your target companies are concentrated (Bangalore, Mumbai, Delhi NCR, Hyderabad, Pune, Chennai)</li>
<li><strong>Company headcount growth</strong> — Growing companies are more likely to buy new tools. Filter for companies with 10%+ headcount growth</li>
<li><strong>Posted on LinkedIn in the last 30 days</strong> — Active LinkedIn users are more likely to respond to outreach</li>
<li><strong>Changed jobs in the last 90 days</strong> — New hires in decision-making roles actively evaluate vendors and are 3x more likely to buy</li>
</ul>

<h3>Finding Email Addresses</h3>
<p>Once you have a list of prospects from LinkedIn, find their business email addresses using:</p>
<ul>
<li><strong>Company website contact pages</strong> — Many Indian companies list team email addresses on their About or Contact page</li>
<li><strong>Email pattern guessing + verification</strong> — Indian companies commonly use firstname@company.com or firstname.lastname@company.com. Guess the pattern and verify it with an email verification tool before sending</li>
<li><strong>LinkedIn connection and ask</strong> — Connect with the prospect first, build rapport with a few genuine interactions, then ask for their email to share relevant content</li>
</ul>

<h2>Method 2: Website Visitor Identification</h2>
<p>People visiting your website are already interested in what you offer. Identify them and add them to your outreach list:</p>
<ul>
<li><strong>Reverse IP lookup tools</strong> — Services like Clearbit Reveal or Leadfeeder identify the companies visiting your website (not individuals, which keeps it DPDP-compliant). Cross-reference with LinkedIn to find the right contact</li>
<li><strong>Gated content</strong> — Offer valuable resources (Indian market reports, compliance checklists, ROI calculators) behind a registration form. The visitor gives you their email in exchange for content they genuinely want</li>
<li><strong>Exit-intent popups</strong> — Catch visitors who are about to leave with a "Get our free guide to [specific topic]" popup. In the Indian B2B context, templates and checklists convert better than generic whitepapers</li>
</ul>

<h2>Method 3: Industry Events and Conferences</h2>
<p>India's B2B conference circuit is thriving, and events are one of the highest-quality lead sources available:</p>
<ul>
<li><strong>SaaS conferences</strong> — SaaSBOOMi, NASSCOM Product Conclave, TiE events, and Startup Grind chapters across Indian cities</li>
<li><strong>Industry-specific events</strong> — Every industry has its annual conferences. Fintech: Global Fintech Fest; HR: SHRM India; Marketing: ad:tech India</li>
<li><strong>Collect business cards and follow up within 48 hours</strong> — In India, exchanging business cards is still standard practice at conferences. The follow-up email should reference your conversation specifically</li>
<li><strong>Speak at events</strong> — Even a 15-minute talk at a local meetup positions you as an expert and generates warmer leads than any booth</li>
</ul>

<h2>Method 4: Content-Driven List Building</h2>
<p>Create content that attracts your ideal customers and capture their information naturally:</p>

<h3>SEO-Optimized Blog Posts</h3>
<p>Write about the problems your target audience searches for. Target India-specific long-tail keywords: "GST invoicing software for freelancers India," "HRMS with attendance tracking India," or "cold email tool for Indian startups." Include a lead magnet (template, checklist, or free tool) within each post.</p>

<h3>Research Reports and Data</h3>
<p>Original research is scarce in the Indian B2B market, which makes it extremely shareable. Conduct a survey of 100+ Indian companies in your target industry, compile the results into a report, and gate it with an email capture form. Indian media outlets and industry blogs will link to original Indian market data, driving organic backlinks and traffic.</p>

<h3>Free Tools and Calculators</h3>
<p>Build a simple free tool related to your product. An ROI calculator, an email subject line grader, a readability scorer, or a pricing comparison tool. Require an email to save or share results. These tools generate leads on autopilot and attract prospects who have the exact problem you solve.</p>

<h2>Method 5: Community-Based Prospecting</h2>
<p>Indian B2B communities are highly active on specific platforms:</p>
<ul>
<li><strong>WhatsApp and Telegram groups</strong> — Industry-specific groups where professionals share advice, job openings, and vendor recommendations. Join, contribute genuinely, and identify prospects through their questions and needs</li>
<li><strong>Reddit India communities</strong> — r/IndianStartups, r/developersIndia, r/IndiaInvestments — these subreddits have active B2B discussions. Answer questions helpfully and mention your solution only when directly relevant</li>
<li><strong>Slack communities</strong> — Communities like SaaSBOOMi Slack, Growth and Marketing India, and ProductFolks have thousands of Indian B2B professionals</li>
<li><strong>Twitter/X</strong> — Follow and engage with Indian startup founders and CXOs. Build relationships through genuine engagement before pitching</li>
</ul>

<h2>Method 6: Referral Mining</h2>
<p>Your existing customers and network are the most underutilized lead source:</p>
<ul>
<li><strong>Ask every happy customer for 3 referrals</strong> — Time the ask right: after a success milestone, not during onboarding. Offer a reward (extended subscription, discount, or gift card) but make it easy — Indian professionals refer more readily when you provide a draft message they can forward</li>
<li><strong>Mine your LinkedIn connections</strong> — You already have first-degree connections. Sort them by your ICP criteria and reach out to relevant ones. A warm message to an existing connection is not cold email — it is networking</li>
<li><strong>Alumni networks</strong> — IIT, IIM, NIT, BITS, and other Indian institution alumni networks are powerful referral channels. Alumni are predisposed to help fellow alumni, especially when the product is genuinely useful</li>
</ul>

<h2>Building and Maintaining Your List</h2>
<p>Once you have prospects, organize and maintain your list for long-term outreach success:</p>
<ul>
<li><strong>Verify every address before adding it</strong> — Use an email verification service; never add an address to your outreach list without confirming it is valid and deliverable</li>
<li><strong>Segment by ICP criteria</strong> — Tag prospects by industry, company size, role, and source. Different segments get different messaging</li>
<li><strong>Clean quarterly</strong> — Remove bounced addresses, unsubscribes, and non-engagers every three months to keep your list healthy</li>
<li><strong>Track source quality</strong> — Measure reply rates and meeting-booked rates by source (LinkedIn, events, content, referral) to focus your list-building effort on what works best</li>
</ul>

<h2>The Bottom Line</h2>
<p>Building a cold email list without buying data takes more effort upfront but produces dramatically better results. A list of 500 verified, well-targeted prospects you built yourself will outperform a purchased list of 10,000 random contacts every time. Focus on quality over quantity, respect the DPDP Act, and use tools like DoAide Reach to maximize the return on every address in your list.</p>
`,
    faqs: [
      { q: 'Is it legal to buy email lists in India?', a: 'While not explicitly illegal, buying email lists creates serious legal risk under the Digital Personal Data Protection Act (DPDP Act, 2023). The Act requires legitimate business interest or consent for processing personal data, and penalties for significant non-compliance can reach 250 crore rupees.' },
      { q: 'What is the best way to build a B2B email list in India?', a: 'The best methods are LinkedIn Sales Navigator prospecting, industry events and conferences, content-driven lead magnets with India-specific topics, community-based prospecting in WhatsApp and Slack groups, and referral mining from existing customers and alumni networks.' },
      { q: 'How many prospects do I need in my cold email list?', a: 'Quality matters more than quantity. A list of 500 verified, well-targeted prospects built through LinkedIn prospecting and direct research will outperform a purchased list of 10,000 random contacts in both reply rates and meeting bookings.' },
      { q: 'How do I find business email addresses of Indian prospects?', a: 'Check company website contact pages, guess common email patterns like firstname@company.com and verify them with an email verification tool, or connect with prospects on LinkedIn first and ask for their email after building rapport.' },
    ],
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

  useEffect(() => {
    if (!article) return

    const blogPostingSchema = {
      '@context': 'https://schema.org',
      '@type': 'BlogPosting',
      headline: article.title,
      description: article.excerpt,
      datePublished: new Date(article.date).toISOString().split('T')[0],
      author: { '@type': 'Organization', name: 'Apprend Technologies', url: 'https://doaide.com' },
      publisher: { '@type': 'Organization', name: 'DoAide Reach', url: 'https://reach.doaide.com' },
      mainEntityOfPage: { '@type': 'WebPage', '@id': `https://reach.doaide.com/blog/${article.slug}` },
    }

    const faqSchema = article.faqs?.length
      ? {
          '@context': 'https://schema.org',
          '@type': 'FAQPage',
          mainEntity: article.faqs.map((f) => ({
            '@type': 'Question',
            name: f.q,
            acceptedAnswer: { '@type': 'Answer', text: f.a },
          })),
        }
      : null

    const scripts = []

    const s1 = document.createElement('script')
    s1.type = 'application/ld+json'
    s1.textContent = JSON.stringify(blogPostingSchema)
    document.head.appendChild(s1)
    scripts.push(s1)

    if (faqSchema) {
      const s2 = document.createElement('script')
      s2.type = 'application/ld+json'
      s2.textContent = JSON.stringify(faqSchema)
      document.head.appendChild(s2)
      scripts.push(s2)
    }

    document.title = `${article.title} | DoAide Reach Blog`

    return () => {
      scripts.forEach((s) => s.remove())
      document.title = 'DoAide Reach — Email Outreach Automation'
    }
  }, [article])

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
