-- Migration: add platform column to analyst_picks
-- Run this in your Supabase SQL Editor (Project > SQL Editor > New query)
-- Safe to run multiple times (IF NOT EXISTS guard)

ALTER TABLE analyst_picks
  ADD COLUMN IF NOT EXISTS platform text;
