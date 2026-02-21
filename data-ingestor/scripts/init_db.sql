-- NSE Market Data - TimescaleDB Initialization
-- =============================================

-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ===== INSTRUMENTS TABLE =====
CREATE TABLE IF NOT EXISTS instruments (
    instrument_id   BIGINT PRIMARY KEY,
    exchange        VARCHAR(10) NOT NULL,
    segment         VARCHAR(10) NOT NULL,
    symbol          VARCHAR(50) NOT NULL,
    name            VARCHAR(200),
    instrument_type VARCHAR(20),
    expiry_date     DATE,
    strike_price    DECIMAL(15,2),
    option_type     VARCHAR(5),
    lot_size        INT,
    tick_size       DECIMAL(10,4),
    is_active       BOOLEAN DEFAULT true,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_instruments_symbol ON instruments(symbol);
CREATE INDEX IF NOT EXISTS idx_instruments_type_expiry ON instruments(instrument_type, expiry_date);

-- ===== TICK DATA (Hypertable) =====
CREATE TABLE IF NOT EXISTS tick_data (
    time                TIMESTAMPTZ NOT NULL,
    instrument_id       BIGINT NOT NULL,
    ltp                 DECIMAL(15,2),
    ltq                 BIGINT,
    total_traded_volume BIGINT,
    open_price          DECIMAL(15,2),
    high_price          DECIMAL(15,2),
    low_price           DECIMAL(15,2),
    close_price         DECIMAL(15,2),
    best_bid_price      DECIMAL(15,2),
    best_bid_qty        BIGINT,
    best_ask_price      DECIMAL(15,2),
    best_ask_qty        BIGINT,
    bid_price_1 DECIMAL(15,2), bid_qty_1 BIGINT,
    bid_price_2 DECIMAL(15,2), bid_qty_2 BIGINT,
    bid_price_3 DECIMAL(15,2), bid_qty_3 BIGINT,
    bid_price_4 DECIMAL(15,2), bid_qty_4 BIGINT,
    bid_price_5 DECIMAL(15,2), bid_qty_5 BIGINT,
    ask_price_1 DECIMAL(15,2), ask_qty_1 BIGINT,
    ask_price_2 DECIMAL(15,2), ask_qty_2 BIGINT,
    ask_price_3 DECIMAL(15,2), ask_qty_3 BIGINT,
    ask_price_4 DECIMAL(15,2), ask_qty_4 BIGINT,
    ask_price_5 DECIMAL(15,2), ask_qty_5 BIGINT
);
SELECT create_hypertable('tick_data', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_tick_instrument ON tick_data(instrument_id, time DESC);

-- ===== OPEN INTEREST TABLE (Hypertable) =====
-- OI data comes from MQTT feed (prod/marketfeed/oi/v1/)
-- Binary packet: 16 bytes = openInterest, dayHighOi, dayLowOi, previousOi
-- change_in_oi is computed as openInterest - previousOi
CREATE TABLE IF NOT EXISTS oi_data (
    time            TIMESTAMPTZ NOT NULL,
    instrument_id   BIGINT NOT NULL,
    open_interest   BIGINT,
    change_in_oi    BIGINT,
    day_high_oi     BIGINT,
    day_low_oi      BIGINT,
    previous_oi     BIGINT,
    ltp             DECIMAL(15,2),
    volume          BIGINT
);
SELECT create_hypertable('oi_data', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_oi_instrument ON oi_data(instrument_id, time DESC);

-- ===== HISTORICAL OHLCV TABLE (Hypertable) - For backtesting =====
CREATE TABLE IF NOT EXISTS historical_ohlcv (
    time            TIMESTAMPTZ NOT NULL,
    instrument_id   BIGINT NOT NULL,
    interval_type   VARCHAR(10) NOT NULL,
    open_price      DECIMAL(15,2),
    high_price      DECIMAL(15,2),
    low_price       DECIMAL(15,2),
    close_price     DECIMAL(15,2),
    volume          BIGINT,
    oi              BIGINT
);
SELECT create_hypertable('historical_ohlcv', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_historical_instrument ON historical_ohlcv(instrument_id, interval_type, time DESC);

-- ===== COMPRESSION POLICIES =====
ALTER TABLE tick_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_id',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('tick_data', INTERVAL '7 days', if_not_exists => TRUE);

ALTER TABLE oi_data SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_id',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('oi_data', INTERVAL '30 days', if_not_exists => TRUE);

ALTER TABLE historical_ohlcv SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'instrument_id',
    timescaledb.compress_orderby = 'time DESC'
);
SELECT add_compression_policy('historical_ohlcv', INTERVAL '30 days', if_not_exists => TRUE);

-- ===== RETENTION (raw tick data only) =====
SELECT add_retention_policy('tick_data', INTERVAL '90 days', if_not_exists => TRUE);

-- ===== CONTINUOUS AGGREGATES =====
CREATE MATERIALIZED VIEW IF NOT EXISTS ohlcv_1min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    instrument_id,
    first(ltp, time)  AS open_price,
    max(ltp)           AS high_price,
    min(ltp)           AS low_price,
    last(ltp, time)    AS close_price,
    last(total_traded_volume, time) AS volume
FROM tick_data
GROUP BY bucket, instrument_id
WITH NO DATA;

-- Refresh policy for 1-min candles
SELECT add_continuous_aggregate_policy('ohlcv_1min',
    start_offset => INTERVAL '1 hour',
    end_offset => INTERVAL '1 minute',
    schedule_interval => INTERVAL '1 minute',
    if_not_exists => TRUE
);

-- ===== TICK DATA VALIDATION TRIGGER =====
-- Corrects LTP outside bid/ask spread and swaps inverted bid/ask.
-- Skips correction when bid/ask are 0 (INDEX instruments like NIFTY).
CREATE OR REPLACE FUNCTION public.validate_tick_data()
RETURNS trigger LANGUAGE plpgsql AS
$function$
BEGIN
    IF NEW.best_bid_price IS NOT NULL AND NEW.best_ask_price IS NOT NULL
       AND NEW.best_bid_price > 0 AND NEW.best_ask_price > 0 THEN
        -- Fix LTP outside spread
        IF NEW.ltp < NEW.best_bid_price OR NEW.ltp > NEW.best_ask_price THEN
            NEW.ltp := (NEW.best_bid_price + NEW.best_ask_price) / 2;
        END IF;

        -- Fix bid > ask
        IF NEW.best_bid_price > NEW.best_ask_price THEN
            NEW.best_bid_price := NEW.best_bid_price + NEW.best_ask_price;
            NEW.best_ask_price := NEW.best_bid_price - NEW.best_ask_price;
            NEW.best_bid_price := NEW.best_bid_price - NEW.best_ask_price;
        END IF;
    END IF;

    RETURN NEW;
END;
$function$;

DROP TRIGGER IF EXISTS tick_data_validation ON tick_data;
CREATE TRIGGER tick_data_validation
    BEFORE INSERT OR UPDATE ON tick_data
    FOR EACH ROW EXECUTE FUNCTION validate_tick_data();

-- Log init completion
DO $$
BEGIN
    RAISE NOTICE 'NSE Market Data schema initialized successfully';
END $$;
