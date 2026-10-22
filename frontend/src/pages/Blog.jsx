import { useEffect } from 'react'
import { Link, useParams } from 'react-router-dom'
import WhatsAppShare from '../components/WhatsAppShare'
import { ARTICLES } from '../content/articles'

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

    // The title and meta tags come from seo.js via SeoHead, like every page.
    return () => {
      scripts.forEach((s) => s.remove())
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
        <div className="blog-share">
          <p>Found this useful? Send it to someone who does outreach.</p>
          <WhatsAppShare text={`${article.title} — https://reach.doaide.com/blog/${article.slug}`} />
        </div>
      </div>
    </div>
  )
}

export { BlogIndex, BlogPost, ARTICLES }
