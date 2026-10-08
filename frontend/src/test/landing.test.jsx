import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '../context/AuthContext'
import Landing from '../pages/Landing'

function renderLanding() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <Landing />
      </AuthProvider>
    </MemoryRouter>,
  )
}

vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal()
  return {
    ...actual,
    default: {
      ...actual.default,
      me: vi.fn().mockRejectedValue(new Error('no session')),
      firm: vi.fn().mockRejectedValue(new Error('no session')),
    },
    getToken: vi.fn().mockReturnValue(null),
    setToken: vi.fn(),
  }
})

describe('Landing page', () => {
  it('renders the hero title', async () => {
    renderLanding()
    expect(await screen.findByText('Outreach that actually works.')).toBeInTheDocument()
  })

  it('renders the CTA buttons', async () => {
    renderLanding()
    const ctas = await screen.findAllByText('Get started free')
    expect(ctas.length).toBeGreaterThanOrEqual(1)
  })

  it('renders testimonials section', async () => {
    renderLanding()
    expect(await screen.findByText('What founders are saying')).toBeInTheDocument()
    expect(screen.getByText('Priya Sharma')).toBeInTheDocument()
    expect(screen.getByText('Arjun Mehta')).toBeInTheDocument()
    expect(screen.getByText('Kavita Reddy')).toBeInTheDocument()
  })

  it('renders FAQ section', async () => {
    renderLanding()
    expect(await screen.findByText('Frequently Asked Questions')).toBeInTheDocument()
    expect(screen.getByText('What is DoAide Reach?')).toBeInTheDocument()
    expect(screen.getByText('Is DoAide Reach free to use?')).toBeInTheDocument()
  })

  it('renders blog section', async () => {
    renderLanding()
    expect(await screen.findByText('From the blog')).toBeInTheDocument()
    expect(screen.getByText('Email Outreach Automation: The Complete Guide for 2026')).toBeInTheDocument()
  })

  it('renders the final CTA', async () => {
    renderLanding()
    expect(await screen.findByText('Ready to automate your outreach?')).toBeInTheDocument()
  })

  it('renders feature descriptions', async () => {
    renderLanding()
    expect(await screen.findByText(/AI-crafted personalized emails/)).toBeInTheDocument()
  })
})
