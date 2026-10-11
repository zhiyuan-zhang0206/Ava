"""Fresh Gateway executable; embedding callers own their logging."""

if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    from gateway.app import main

    main()
