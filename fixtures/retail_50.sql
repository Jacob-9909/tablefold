-- Fixture: a 50-table retail schema in the shape a pg_dump produces.
-- Foreign keys are declared as trailing ALTER TABLE statements (pg_dump style)
-- with a handful of inline REFERENCES, so the DDL introspector is exercised on
-- both forms.

-- ── customer domain ──────────────────────────────────────────────────────────

CREATE TABLE customers (
    id            bigint PRIMARY KEY,
    email         varchar(255) NOT NULL,
    first_name    varchar(100),
    last_name     varchar(100),
    tier_id       bigint,
    segment_id    bigint,
    created_at    timestamp NOT NULL,
    lifetime_value numeric(14,2),
    is_active     boolean
);

CREATE TABLE customer_tiers (
    id         bigint PRIMARY KEY,
    code       varchar(32) NOT NULL,
    label      varchar(100),
    discount_pct numeric(5,2)
);

CREATE TABLE customer_segments (
    id    bigint PRIMARY KEY,
    name  varchar(100),
    description text
);

CREATE TABLE addresses (
    id           bigint PRIMARY KEY,
    customer_id  bigint NOT NULL,
    country_id   bigint,
    line1        varchar(255),
    line2        varchar(255),
    city         varchar(100),
    postal_code  varchar(32),
    is_default   boolean
);

CREATE TABLE countries (
    id      bigint PRIMARY KEY,
    iso2    varchar(2) NOT NULL,
    name    varchar(100),
    region_id bigint
);

CREATE TABLE regions (
    id   bigint PRIMARY KEY,
    name varchar(100)
);

CREATE TABLE customer_contacts (
    id          bigint PRIMARY KEY,
    customer_id bigint NOT NULL,
    channel     varchar(32),
    value       varchar(255),
    verified_at timestamp
);

CREATE TABLE customer_consents (
    id          bigint PRIMARY KEY,
    customer_id bigint NOT NULL,
    purpose     varchar(64),
    granted     boolean,
    granted_at  timestamp
);

CREATE TABLE loyalty_accounts (
    id           bigint PRIMARY KEY,
    customer_id  bigint NOT NULL,
    points_balance integer,
    enrolled_at  timestamp
);

CREATE TABLE loyalty_transactions (
    id                 bigint PRIMARY KEY,
    loyalty_account_id bigint NOT NULL,
    points_delta       integer,
    reason             varchar(64),
    occurred_at        timestamp
);

-- ── catalog domain ───────────────────────────────────────────────────────────

CREATE TABLE products (
    id           bigint PRIMARY KEY,
    sku          varchar(64) NOT NULL,
    name         varchar(255),
    category_id  bigint,
    brand_id     bigint,
    supplier_id  bigint,
    list_price   numeric(12,2),
    cost_price   numeric(12,2),
    weight_grams integer,
    launched_at  date,
    is_discontinued boolean
);

CREATE TABLE categories (
    id        bigint PRIMARY KEY,
    name      varchar(100),
    parent_id bigint,
    depth     integer
);

CREATE TABLE brands (
    id      bigint PRIMARY KEY,
    name    varchar(100),
    country_id bigint
);

CREATE TABLE suppliers (
    id         bigint PRIMARY KEY,
    name       varchar(255),
    country_id bigint,
    lead_time_days integer
);

CREATE TABLE product_variants (
    id         bigint PRIMARY KEY,
    product_id bigint NOT NULL,
    variant_sku varchar(64),
    color      varchar(50),
    size       varchar(50),
    price_delta numeric(10,2)
);

CREATE TABLE product_images (
    id         bigint PRIMARY KEY,
    product_id bigint NOT NULL,
    url        text,
    position   integer
);

CREATE TABLE product_attributes (
    id         bigint PRIMARY KEY,
    product_id bigint NOT NULL,
    key        varchar(64),
    value      varchar(255)
);

CREATE TABLE product_reviews (
    id          bigint PRIMARY KEY,
    product_id  bigint NOT NULL,
    customer_id bigint,
    rating      integer,
    body        text,
    created_at  timestamp
);

CREATE TABLE price_lists (
    id       bigint PRIMARY KEY,
    name     varchar(100),
    currency varchar(3),
    valid_from date,
    valid_to date
);

CREATE TABLE price_list_items (
    id            bigint PRIMARY KEY,
    price_list_id bigint NOT NULL,
    product_id    bigint NOT NULL,
    unit_price    numeric(12,2)
);

-- ── sales domain ─────────────────────────────────────────────────────────────

CREATE TABLE orders (
    id              bigint PRIMARY KEY,
    order_number    varchar(32) NOT NULL,
    customer_id     bigint NOT NULL,
    address_id      bigint,
    channel_id      bigint,
    store_id        bigint,
    currency        varchar(3),
    status          integer,
    subtotal        numeric(14,2),
    discount_total  numeric(14,2),
    tax_total       numeric(14,2),
    shipping_total  numeric(14,2),
    grand_total     numeric(14,2),
    placed_at       timestamp NOT NULL,
    fulfilled_at    timestamp,
    cancelled_at    timestamp
);

CREATE TABLE order_items (
    id          bigint PRIMARY KEY,
    order_id    bigint NOT NULL,
    product_id  bigint NOT NULL,
    variant_id  bigint,
    quantity    integer NOT NULL,
    unit_price  numeric(12,2),
    line_total  numeric(14,2),
    discount    numeric(12,2)
);

CREATE TABLE order_discounts (
    id         bigint PRIMARY KEY,
    order_id   bigint NOT NULL,
    promotion_id bigint,
    amount     numeric(12,2),
    code       varchar(64)
);

CREATE TABLE payments (
    id            bigint PRIMARY KEY,
    order_id      bigint NOT NULL,
    method_id     bigint,
    amount        numeric(14,2),
    currency      varchar(3),
    status        integer,
    captured_at   timestamp
);

CREATE TABLE payment_methods (
    id    bigint PRIMARY KEY,
    code  varchar(32),
    label varchar(100)
);

CREATE TABLE refunds (
    id         bigint PRIMARY KEY,
    payment_id bigint NOT NULL,
    amount     numeric(14,2),
    reason     varchar(255),
    refunded_at timestamp
);

CREATE TABLE sales_channels (
    id   bigint PRIMARY KEY,
    code varchar(32),
    name varchar(100)
);

CREATE TABLE stores (
    id         bigint PRIMARY KEY,
    code       varchar(32),
    name       varchar(150),
    country_id bigint,
    opened_on  date
);

CREATE TABLE carts (
    id          bigint PRIMARY KEY,
    customer_id bigint,
    created_at  timestamp,
    converted_order_id bigint
);

CREATE TABLE cart_items (
    id         bigint PRIMARY KEY,
    cart_id    bigint NOT NULL,
    product_id bigint NOT NULL,
    quantity   integer
);

-- ── fulfilment / inventory domain ────────────────────────────────────────────

CREATE TABLE shipments (
    id           bigint PRIMARY KEY,
    order_id     bigint NOT NULL,
    warehouse_id bigint,
    carrier_id   bigint,
    tracking_no  varchar(64),
    shipped_at   timestamp,
    delivered_at timestamp,
    weight_grams integer,
    cost         numeric(12,2)
);

CREATE TABLE shipment_items (
    id            bigint PRIMARY KEY,
    shipment_id   bigint NOT NULL,
    order_item_id bigint NOT NULL,
    quantity      integer
);

CREATE TABLE warehouses (
    id         bigint PRIMARY KEY,
    code       varchar(32),
    name       varchar(150),
    country_id bigint
);

CREATE TABLE carriers (
    id   bigint PRIMARY KEY,
    code varchar(32),
    name varchar(100)
);

CREATE TABLE inventory_levels (
    id           bigint PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    product_id   bigint NOT NULL,
    on_hand      integer,
    reserved     integer,
    reorder_point integer,
    updated_at   timestamp
);

CREATE TABLE inventory_movements (
    id           bigint PRIMARY KEY,
    warehouse_id bigint NOT NULL,
    product_id   bigint NOT NULL,
    quantity_delta integer,
    movement_type varchar(32),
    occurred_at  timestamp
);

CREATE TABLE purchase_orders (
    id          bigint PRIMARY KEY,
    supplier_id bigint NOT NULL,
    warehouse_id bigint,
    status      integer,
    total_cost  numeric(14,2),
    ordered_at  timestamp,
    received_at timestamp
);

CREATE TABLE purchase_order_items (
    id                bigint PRIMARY KEY,
    purchase_order_id bigint NOT NULL,
    product_id        bigint NOT NULL,
    quantity          integer,
    unit_cost         numeric(12,2)
);

CREATE TABLE returns (
    id          bigint PRIMARY KEY,
    order_id    bigint NOT NULL,
    reason_id   bigint,
    status      integer,
    received_at timestamp,
    refund_total numeric(14,2)
);

CREATE TABLE return_reasons (
    id    bigint PRIMARY KEY,
    code  varchar(32),
    label varchar(100)
);

-- ── marketing domain ─────────────────────────────────────────────────────────

CREATE TABLE campaigns (
    id         bigint PRIMARY KEY,
    name       varchar(150),
    channel_id bigint,
    budget     numeric(14,2),
    started_on date,
    ended_on   date
);

CREATE TABLE promotions (
    id          bigint PRIMARY KEY,
    campaign_id bigint,
    code        varchar(64),
    discount_pct numeric(5,2),
    valid_from  date,
    valid_to    date
);

CREATE TABLE campaign_touches (
    id          bigint PRIMARY KEY,
    campaign_id bigint NOT NULL,
    customer_id bigint NOT NULL,
    touched_at  timestamp,
    medium      varchar(32)
);

CREATE TABLE email_sends (
    id          bigint PRIMARY KEY,
    campaign_id bigint,
    customer_id bigint NOT NULL,
    sent_at     timestamp,
    opened_at   timestamp,
    clicked_at  timestamp
);

CREATE TABLE web_sessions (
    id          bigint PRIMARY KEY,
    customer_id bigint,
    started_at  timestamp,
    ended_at    timestamp,
    device      varchar(32),
    referrer    varchar(255)
);

CREATE TABLE page_views (
    id         bigint PRIMARY KEY,
    session_id bigint NOT NULL,
    path       varchar(255),
    viewed_at  timestamp,
    duration_ms integer
);

-- ── finance / reference domain ───────────────────────────────────────────────

CREATE TABLE invoices (
    id         bigint PRIMARY KEY,
    order_id   bigint NOT NULL,
    number     varchar(32),
    issued_on  date,
    due_on     date,
    total      numeric(14,2)
);

CREATE TABLE invoice_lines (
    id         bigint PRIMARY KEY,
    invoice_id bigint NOT NULL,
    description varchar(255),
    amount     numeric(14,2),
    tax_rate_id bigint
);

CREATE TABLE tax_rates (
    id         bigint PRIMARY KEY,
    country_id bigint,
    code       varchar(32),
    rate       numeric(6,4)
);

CREATE TABLE currencies (
    code  varchar(3) PRIMARY KEY,
    name  varchar(50),
    minor_unit integer
);

CREATE TABLE exchange_rates (
    id            bigint PRIMARY KEY,
    base_code     varchar(3) NOT NULL,
    quote_code    varchar(3) NOT NULL,
    rate          numeric(18,8),
    rated_on      date
);

CREATE TABLE employees (
    id         bigint PRIMARY KEY,
    store_id   bigint,
    full_name  varchar(150),
    role       varchar(64),
    hired_on   date
);

CREATE TABLE audit_events (
    id         bigint PRIMARY KEY,
    entity     varchar(64),
    entity_id  bigint,
    action     varchar(32),
    actor_id   bigint,
    occurred_at timestamp
);

-- ── foreign keys ─────────────────────────────────────────────────────────────

ALTER TABLE customers ADD CONSTRAINT fk_customers_tier FOREIGN KEY (tier_id) REFERENCES customer_tiers (id);
ALTER TABLE customers ADD CONSTRAINT fk_customers_segment FOREIGN KEY (segment_id) REFERENCES customer_segments (id);
ALTER TABLE addresses ADD CONSTRAINT fk_addresses_customer FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE addresses ADD CONSTRAINT fk_addresses_country FOREIGN KEY (country_id) REFERENCES countries (id);
ALTER TABLE countries ADD CONSTRAINT fk_countries_region FOREIGN KEY (region_id) REFERENCES regions (id);
ALTER TABLE customer_contacts ADD CONSTRAINT fk_contacts_customer FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE customer_consents ADD CONSTRAINT fk_consents_customer FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE loyalty_accounts ADD CONSTRAINT fk_loyalty_customer FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE loyalty_transactions ADD CONSTRAINT fk_loyalty_tx_account FOREIGN KEY (loyalty_account_id) REFERENCES loyalty_accounts (id);

ALTER TABLE products ADD CONSTRAINT fk_products_category FOREIGN KEY (category_id) REFERENCES categories (id);
ALTER TABLE products ADD CONSTRAINT fk_products_brand FOREIGN KEY (brand_id) REFERENCES brands (id);
ALTER TABLE products ADD CONSTRAINT fk_products_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (id);
ALTER TABLE categories ADD CONSTRAINT fk_categories_parent FOREIGN KEY (parent_id) REFERENCES categories (id);
ALTER TABLE brands ADD CONSTRAINT fk_brands_country FOREIGN KEY (country_id) REFERENCES countries (id);
ALTER TABLE suppliers ADD CONSTRAINT fk_suppliers_country FOREIGN KEY (country_id) REFERENCES countries (id);
ALTER TABLE product_variants ADD CONSTRAINT fk_variants_product FOREIGN KEY (product_id) REFERENCES products (id);
ALTER TABLE product_images ADD CONSTRAINT fk_images_product FOREIGN KEY (product_id) REFERENCES products (id);
ALTER TABLE product_attributes ADD CONSTRAINT fk_attributes_product FOREIGN KEY (product_id) REFERENCES products (id);
ALTER TABLE product_reviews ADD CONSTRAINT fk_reviews_product FOREIGN KEY (product_id) REFERENCES products (id);
ALTER TABLE product_reviews ADD CONSTRAINT fk_reviews_customer FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE price_list_items ADD CONSTRAINT fk_pli_list FOREIGN KEY (price_list_id) REFERENCES price_lists (id);
ALTER TABLE price_list_items ADD CONSTRAINT fk_pli_product FOREIGN KEY (product_id) REFERENCES products (id);

ALTER TABLE orders ADD CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE orders ADD CONSTRAINT fk_orders_address FOREIGN KEY (address_id) REFERENCES addresses (id);
ALTER TABLE orders ADD CONSTRAINT fk_orders_channel FOREIGN KEY (channel_id) REFERENCES sales_channels (id);
ALTER TABLE orders ADD CONSTRAINT fk_orders_store FOREIGN KEY (store_id) REFERENCES stores (id);
ALTER TABLE order_items ADD CONSTRAINT fk_items_order FOREIGN KEY (order_id) REFERENCES orders (id);
ALTER TABLE order_items ADD CONSTRAINT fk_items_product FOREIGN KEY (product_id) REFERENCES products (id);
ALTER TABLE order_items ADD CONSTRAINT fk_items_variant FOREIGN KEY (variant_id) REFERENCES product_variants (id);
ALTER TABLE order_discounts ADD CONSTRAINT fk_odisc_order FOREIGN KEY (order_id) REFERENCES orders (id);
ALTER TABLE order_discounts ADD CONSTRAINT fk_odisc_promotion FOREIGN KEY (promotion_id) REFERENCES promotions (id);
ALTER TABLE payments ADD CONSTRAINT fk_payments_order FOREIGN KEY (order_id) REFERENCES orders (id);
ALTER TABLE payments ADD CONSTRAINT fk_payments_method FOREIGN KEY (method_id) REFERENCES payment_methods (id);
ALTER TABLE refunds ADD CONSTRAINT fk_refunds_payment FOREIGN KEY (payment_id) REFERENCES payments (id);
ALTER TABLE stores ADD CONSTRAINT fk_stores_country FOREIGN KEY (country_id) REFERENCES countries (id);
ALTER TABLE carts ADD CONSTRAINT fk_carts_customer FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE carts ADD CONSTRAINT fk_carts_order FOREIGN KEY (converted_order_id) REFERENCES orders (id);
ALTER TABLE cart_items ADD CONSTRAINT fk_cart_items_cart FOREIGN KEY (cart_id) REFERENCES carts (id);
ALTER TABLE cart_items ADD CONSTRAINT fk_cart_items_product FOREIGN KEY (product_id) REFERENCES products (id);

ALTER TABLE shipments ADD CONSTRAINT fk_shipments_order FOREIGN KEY (order_id) REFERENCES orders (id);
ALTER TABLE shipments ADD CONSTRAINT fk_shipments_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (id);
ALTER TABLE shipments ADD CONSTRAINT fk_shipments_carrier FOREIGN KEY (carrier_id) REFERENCES carriers (id);
ALTER TABLE shipment_items ADD CONSTRAINT fk_ship_items_shipment FOREIGN KEY (shipment_id) REFERENCES shipments (id);
ALTER TABLE shipment_items ADD CONSTRAINT fk_ship_items_order_item FOREIGN KEY (order_item_id) REFERENCES order_items (id);
ALTER TABLE warehouses ADD CONSTRAINT fk_warehouses_country FOREIGN KEY (country_id) REFERENCES countries (id);
ALTER TABLE inventory_levels ADD CONSTRAINT fk_inv_levels_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (id);
ALTER TABLE inventory_levels ADD CONSTRAINT fk_inv_levels_product FOREIGN KEY (product_id) REFERENCES products (id);
ALTER TABLE inventory_movements ADD CONSTRAINT fk_inv_mov_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (id);
ALTER TABLE inventory_movements ADD CONSTRAINT fk_inv_mov_product FOREIGN KEY (product_id) REFERENCES products (id);
ALTER TABLE purchase_orders ADD CONSTRAINT fk_po_supplier FOREIGN KEY (supplier_id) REFERENCES suppliers (id);
ALTER TABLE purchase_orders ADD CONSTRAINT fk_po_warehouse FOREIGN KEY (warehouse_id) REFERENCES warehouses (id);
ALTER TABLE purchase_order_items ADD CONSTRAINT fk_poi_po FOREIGN KEY (purchase_order_id) REFERENCES purchase_orders (id);
ALTER TABLE purchase_order_items ADD CONSTRAINT fk_poi_product FOREIGN KEY (product_id) REFERENCES products (id);
ALTER TABLE returns ADD CONSTRAINT fk_returns_order FOREIGN KEY (order_id) REFERENCES orders (id);
ALTER TABLE returns ADD CONSTRAINT fk_returns_reason FOREIGN KEY (reason_id) REFERENCES return_reasons (id);

ALTER TABLE campaigns ADD CONSTRAINT fk_campaigns_channel FOREIGN KEY (channel_id) REFERENCES sales_channels (id);
ALTER TABLE promotions ADD CONSTRAINT fk_promotions_campaign FOREIGN KEY (campaign_id) REFERENCES campaigns (id);
ALTER TABLE campaign_touches ADD CONSTRAINT fk_touches_campaign FOREIGN KEY (campaign_id) REFERENCES campaigns (id);
ALTER TABLE campaign_touches ADD CONSTRAINT fk_touches_customer FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE email_sends ADD CONSTRAINT fk_email_campaign FOREIGN KEY (campaign_id) REFERENCES campaigns (id);
ALTER TABLE email_sends ADD CONSTRAINT fk_email_customer FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE web_sessions ADD CONSTRAINT fk_sessions_customer FOREIGN KEY (customer_id) REFERENCES customers (id);
ALTER TABLE page_views ADD CONSTRAINT fk_page_views_session FOREIGN KEY (session_id) REFERENCES web_sessions (id);

ALTER TABLE invoices ADD CONSTRAINT fk_invoices_order FOREIGN KEY (order_id) REFERENCES orders (id);
ALTER TABLE invoice_lines ADD CONSTRAINT fk_inv_lines_invoice FOREIGN KEY (invoice_id) REFERENCES invoices (id);
ALTER TABLE invoice_lines ADD CONSTRAINT fk_inv_lines_tax FOREIGN KEY (tax_rate_id) REFERENCES tax_rates (id);
ALTER TABLE tax_rates ADD CONSTRAINT fk_tax_country FOREIGN KEY (country_id) REFERENCES countries (id);
ALTER TABLE employees ADD CONSTRAINT fk_employees_store FOREIGN KEY (store_id) REFERENCES stores (id);
