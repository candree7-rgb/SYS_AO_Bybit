import { NextResponse } from 'next/server'

const MAINTENANCE_PASSWORD = process.env.MAINTENANCE_PASSWORD || 'Laundre'

export async function POST(request: Request) {
  try {
    const { password } = await request.json()

    if (password === MAINTENANCE_PASSWORD) {
      const response = NextResponse.json({ success: true })
      response.cookies.set('maintenance_auth', 'authenticated', {
        httpOnly: true,
        secure: process.env.NODE_ENV === 'production',
        sameSite: 'lax',
        maxAge: 60 * 60 * 24 * 30, // 30 days
        path: '/',
      })
      return response
    }

    return NextResponse.json({ error: 'Wrong password' }, { status: 401 })
  } catch {
    return NextResponse.json({ error: 'Invalid request' }, { status: 400 })
  }
}

export const dynamic = 'force-dynamic'
