-- Migration: Add price history tracking to listings
-- Date: 2026-02-08
-- Description: Adds columns for tracking price changes, sold status, and days on market

-- Add new columns to listings table
ALTER TABLE listings ADD COLUMN first_price_eur FLOAT;
ALTER TABLE listings ADD COLUMN price_changes INTEGER DEFAULT 0;
ALTER TABLE listings ADD COLUMN is_sold BOOLEAN DEFAULT FALSE;
ALTER TABLE listings ADD COLUMN sold_date DATETIME;
ALTER TABLE listings ADD COLUMN days_on_market INTEGER;

-- Backfill first_price_eur from current price_eur for existing listings
UPDATE listings SET first_price_eur = price_eur WHERE first_price_eur IS NULL;

-- Backfill days_on_market for existing listings
UPDATE listings SET days_on_market = CAST(
    (julianday(COALESCE(sold_date, CURRENT_TIMESTAMP)) - julianday(first_seen)) AS INTEGER
) WHERE days_on_market IS NULL AND first_seen IS NOT NULL;

-- Create index for price history lookups
CREATE INDEX IF NOT EXISTS idx_price_history_listing_recorded 
    ON price_history(listing_id, recorded_at DESC);

-- Create index for sold/active queries
CREATE INDEX IF NOT EXISTS idx_listing_is_sold ON listings(is_sold);
CREATE INDEX IF NOT EXISTS idx_listing_days_on_market ON listings(days_on_market);
