import { useEffect, useState } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'

const FEATURES = [
  { icon: '\u{1F4E7}', title: 'Smart Emails', desc: 'AI-crafted personalized emails that resonate with each recipient' },
  { icon: '\u{1F3AF}', title: 'Targeting', desc: 'Precision audience segmentation by industry, role, and company size' },
  { icon: '\u{1F4CA}', title: 'Campaign AI', desc: 'Real-time analytics with AI recommendations to optimize every send' },
  { icon: '\u{1F504}', title: 'Auto Follow-up', desc: 'Behavior-triggered follow-ups that adapt timing and messaging' },
]

const TESTIMONIALS = [
  {
    quote: 'Reach cut our outreach time by 70%. The AI personalization actually sounds human, and our reply rates tripled in the first month.',
    name: 'Priya Sharma',
    role: 'Founder, GrowthLab India',
  },
  {
    quote: 'We switched from Mailchimp to Reach for cold outreach. The auto follow-up sequences alone paid for themselves in the first week.',
    name: 'Arjun Mehta',
    role: 'Head of Sales, FinStack',
  },
  {
    quote: 'As a solo founder, I needed something that could run campaigns while I focused on product. Reach does exactly that.',
    name: 'Kavita Reddy',
    role: 'CEO, CloudBridge Solutions',
  },
]

const FAQ_ITEMS = [
  {
    q: 'What is DoAide Reach?',
    a: 'DoAide Reach is an AI-powered email outreach automation platform designed for Indian startups and businesses. It helps you create targeted email campaigns, automate follow-ups, and track engagement with built-in analytics.',
  },
  {
    q: 'Is DoAide Reach free to use?',
    a: 'Yes, DoAide Reach offers a free plan that includes core features like email campaign creation, basic targeting, and analytics. Premium plans unlock advanced AI personalization and higher sending limits.',
  },
  {
    q: 'How does the AI personalization work?',
    a: 'DoAide Reach uses AI to analyze your target audience and generate personalized email content for each recipient. It considers industry, role, company size, and previous interactions to craft messages that resonate.',
  },
  {
    q: 'Can I automate follow-up emails?',
    a: 'Yes. You can set up automated follow-up sequences triggered by recipient behavior — opens, clicks, or no response. The AI adjusts follow-up timing and messaging based on engagement patterns.',
  },
  {
    q: 'How is DoAide Reach different from cold email tools?',
    a: 'Unlike generic cold email tools, DoAide Reach is built for the Indian market with features like regional targeting, vernacular support, and pricing optimized for Indian startups.',
  },
  {
    q: 'What analytics does DoAide Reach provide?',
    a: 'Campaign-level and email-level analytics including open rates, click rates, reply rates, bounce rates, and engagement heatmaps. The AI also generates actionable recommendations to improve future campaigns.',
  },
]

const BLOG_ARTICLES = [
  {
    slug: 'email-outreach-automation-guide',
    title: 'Email Outreach Automation: The Complete Guide for 2026',
    excerpt: 'Learn how to set up automated email outreach campaigns that generate replies, not spam complaints.',
  },
  {
    slug: 'cold-email-best-practices-indian-startups',
    title: 'Cold Email Best Practices for Indian Startups',
    excerpt: 'What works (and what gets you blacklisted) when cold emailing in the Indian B2B market.',
  },
  {
    slug: 'outreach-vs-ads-roi-comparison',
    title: 'Outreach vs Ads: Which Delivers Better ROI for Early-Stage Startups?',
    excerpt: 'A data-driven comparison of email outreach and paid advertising for startups with limited budgets.',
  },
]

const DOAIDE_PRODUCTS = [
  { name: 'Desk', url: 'https://desk.doaide.com' },
  { name: 'Jobs', url: 'https://job.doaide.com' },
  { name: '409A', url: 'https://409a.doaide.com' },
  { name: 'GST', url: 'https://gst.doaide.com' },
  { name: 'Pulse', url: 'https://pulse.doaide.com' },
  { name: 'Med', url: 'https://med.doaide.com' },
  { name: 'Realty', url: 'https://realty.doaide.com' },
  { name: 'Reach', url: 'https://reach.doaide.com' },
  { name: 'Trade', url: 'https://trade.doaide.com' },
]

function RobotFace({ size = 32, color }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" width={size} height={size}>
      <line x1="16" y1="6" x2="16" y2="2" stroke={color} strokeWidth="1.5" strokeLinecap="round"/>
      <circle cx="16" cy="1.5" r="1.5" fill={color}/>
      <rect x="5" y="6" width="22" height="17" rx="5" fill={color}/>
      <ellipse cx="11" cy="13" rx="2.5" ry="3" fill="#0A0A0B"/>
      <ellipse cx="21" cy="13" rx="2.5" ry="3" fill="#0A0A0B"/>
      <circle cx="11.5" cy="12.5" r="1" fill={color} opacity="0.6"/>
      <circle cx="21.5" cy="12.5" r="1" fill={color} opacity="0.6"/>
      <path d="M12 19Q16 22 20 19" stroke="#0A0A0B" strokeWidth="1.2" fill="none" strokeLinecap="round"/>
      <rect x="1" y="10" width="4" height="5" rx="2" fill={color} opacity="0.8"/>
      <rect x="27" y="10" width="4" height="5" rx="2" fill={color} opacity="0.8"/>
    </svg>
  )
}

function HeroRobot({ color }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 100" width="120" height="100" className="landing-hero-robot">
      <line x1="60" y1="18" x2="60" y2="6" stroke={color} strokeWidth="2.5" strokeLinecap="round"/>
      <circle cx="60" cy="4" r="3" fill={color} className="landing-antenna-glow"/>
      <rect x="25" y="18" width="70" height="55" rx="16" fill={color}/>
      <ellipse cx="42" cy="40" rx="8" ry="10" fill="#0A0A0B"/>
      <ellipse cx="78" cy="40" rx="8" ry="10" fill="#0A0A0B"/>
      <circle cx="44" cy="38" r="3" fill={color} opacity="0.5"/>
      <circle cx="80" cy="38" r="3" fill={color} opacity="0.5"/>
      <path d="M45 60 Q60 72 75 60" stroke="#0A0A0B" strokeWidth="2.5" fill="none" strokeLinecap="round"/>
      <rect x="5" y="30" width="16" height="18" rx="6" fill={color} opacity="0.8"/>
      <rect x="99" y="30" width="16" height="18" rx="6" fill={color} opacity="0.8"/>
    </svg>
  )
}

const ACCENT = '#D4AF37'
const ACCENT_DARK = '#B8962F'

export default function Landing() {
  const { isAuthenticated, loading } = useAuth()
  const [visible, setVisible] = useState(false)

  useEffect(() => {
    requestAnimationFrame(() => setVisible(true))
  }, [])

  if (loading) return null
  if (isAuthenticated) return <Navigate to="/dashboard" replace />

  return (
    <div className="landing-root" style={{ '--accent': ACCENT, '--accent-dark': ACCENT_DARK }}>
      <div className="landing-bg">
        <div className="landing-orb landing-orb-1" />
        <div className="landing-orb landing-orb-2" />
        <div className="landing-orb landing-orb-3" />
      </div>

      <header className={`landing-header ${visible ? 'landing-visible' : ''}`}>
        <a href="https://doaide.com" className="landing-brand">
          <RobotFace size={28} color={ACCENT} />
          <span className="landing-brand-text">
            Do<em>Aide</em> Reach
          </span>
        </a>
        <div className="landing-header-actions">
          <Link to="/login" className="landing-btn-ghost">Sign in</Link>
          <Link to="/register" className="landing-btn-primary">Get started</Link>
        </div>
      </header>

      <main className={`landing-hero ${visible ? 'landing-visible' : ''}`}>
        <div className="landing-hero-robot-wrap">
          <HeroRobot color={ACCENT} />
        </div>
        <h1 className="landing-title">Outreach that actually works.</h1>
        <div className="landing-cta-group">
          <Link to="/register" className="landing-btn-primary landing-btn-lg">Get started free</Link>
          <Link to="/login" className="landing-btn-ghost landing-btn-lg">Sign in</Link>
        </div>
      </main>

      <section className={`landing-features ${visible ? 'landing-visible' : ''}`}>
        {FEATURES.map((f, i) => (
          <div
            key={f.title}
            className="landing-feature-card"
            style={{ animationDelay: `${0.3 + i * 0.1}s` }}
          >
            <span className="landing-feature-icon">{f.icon}</span>
            <span className="landing-feature-title">{f.title}</span>
            <span className="landing-feature-desc">{f.desc}</span>
          </div>
        ))}
      </section>

      <section className="landing-testimonials">
        <h2 className="landing-section-title">What founders are saying</h2>
        <div className="landing-testimonials-grid">
          {TESTIMONIALS.map((t) => (
            <div key={t.name} className="landing-testimonial-card">
              <p className="landing-testimonial-quote">&ldquo;{t.quote}&rdquo;</p>
              <div className="landing-testimonial-author">
                <span className="landing-testimonial-name">{t.name}</span>
                <span className="landing-testimonial-role">{t.role}</span>
              </div>
            </div>
          ))}
        </div>
      </section>

      <section className="landing-blog">
        <h2 className="landing-section-title">From the blog</h2>
        <div className="landing-blog-grid">
          {BLOG_ARTICLES.map((a) => (
            <Link key={a.slug} to={`/blog/${a.slug}`} className="landing-blog-card">
              <h3 className="landing-blog-title">{a.title}</h3>
              <p className="landing-blog-excerpt">{a.excerpt}</p>
              <span className="landing-blog-read">Read more &rarr;</span>
            </Link>
          ))}
        </div>
      </section>

      <section className="landing-faq">
        <h2 className="landing-section-title">Frequently Asked Questions</h2>
        <div className="landing-faq-list">
          {FAQ_ITEMS.map((item) => (
            <details key={item.q} className="landing-faq-item">
              <summary className="landing-faq-question">{item.q}</summary>
              <p className="landing-faq-answer">{item.a}</p>
            </details>
          ))}
        </div>
      </section>

      <section className="landing-final-cta">
        <h2 className="landing-section-title">Ready to automate your outreach?</h2>
        <p className="landing-final-cta-sub">Start sending smarter emails in minutes. No credit card required.</p>
        <div className="landing-cta-group">
          <Link to="/register" className="landing-btn-primary landing-btn-lg">Get started free</Link>
        </div>
      </section>

      <footer className="landing-footer">
        <div className="landing-footer-products">
          {DOAIDE_PRODUCTS.map((p) => (
            <a key={p.name} href={p.url} className="landing-footer-link">
              {p.name}
            </a>
          ))}
        </div>
        <div className="landing-footer-bottom">
          <a href="https://doaide.com" className="landing-footer-home">
            <RobotFace size={16} color={ACCENT} />
            doaide.com
          </a>
          <span className="landing-footer-copy">&copy; 2026 DoAide</span>
        </div>
      </footer>
    </div>
  )
}
