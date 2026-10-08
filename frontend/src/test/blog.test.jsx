import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { BlogIndex, BlogPost, ARTICLES } from '../pages/Blog'

function renderAt(route) {
  return render(
    <MemoryRouter initialEntries={[route]}>
      <Routes>
        <Route path="/blog" element={<BlogIndex />} />
        <Route path="/blog/:slug" element={<BlogPost />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('Blog index', () => {
  it('renders the blog index page', () => {
    renderAt('/blog')
    expect(screen.getByText('Blog')).toBeInTheDocument()
  })

  it('lists all 3 articles', () => {
    renderAt('/blog')
    for (const article of ARTICLES) {
      expect(screen.getByText(article.title)).toBeInTheDocument()
    }
  })

  it('links back to the main site', () => {
    renderAt('/blog')
    expect(screen.getByText(/Back to DoAide Reach/)).toBeInTheDocument()
  })
})

describe('Blog post', () => {
  it('renders the email outreach automation article', () => {
    renderAt('/blog/email-outreach-automation-guide')
    expect(screen.getByText('Email Outreach Automation: The Complete Guide for 2026')).toBeInTheDocument()
  })

  it('renders the cold email best practices article', () => {
    renderAt('/blog/cold-email-best-practices-indian-startups')
    expect(screen.getByText('Cold Email Best Practices for Indian Startups')).toBeInTheDocument()
  })

  it('renders the outreach vs ads article', () => {
    renderAt('/blog/outreach-vs-ads-roi-comparison')
    expect(screen.getByText(/Outreach vs Ads/)).toBeInTheDocument()
  })

  it('shows 404 for unknown slug', () => {
    renderAt('/blog/nonexistent-article')
    expect(screen.getByText('Article Not Found')).toBeInTheDocument()
  })

  it('has a back link to the blog index', () => {
    renderAt('/blog/email-outreach-automation-guide')
    expect(screen.getByText(/Back to Blog/)).toBeInTheDocument()
  })
})
