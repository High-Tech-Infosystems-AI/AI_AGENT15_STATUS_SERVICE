"""Outgoing webhooks: platform events POSTed to URLs an organisation registers.

Every event the services send to /notifications/event is offered to the
organisation's webhook endpoints (catalog.py lists the events that can be
subscribed to). Deliveries are signed, logged and retried (delivery.py).
"""
