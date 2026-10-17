export interface paths {
    "/api/auth/login": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["login_api_auth_login_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/login-ticket": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["issue_login_ticket_api_auth_login_ticket_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/login-ticket/redeem": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["redeem_login_ticket_api_auth_login_ticket_redeem_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/logout": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["logout_api_auth_logout_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/check": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["check_api_auth_check_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/sessions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["sessions_api_auth_sessions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/auth/sessions/{session_id}/revoke": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["revoke_other_session_api_auth_sessions__session_id__revoke_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/bootstrap": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_bootstrap_api_bootstrap_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_api_agents__agent_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch: operations["patch_agent_api_agents__agent_id__patch"];
        trace?: never;
    };
    "/api/models": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_models_api_models_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agents_api_agents_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/roster": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_roster_api_agents_roster_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_guarded_agents_api_keyed_v1_agents_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/born-chain": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_born_chain_api_agents__agent_id__born_chain_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/to-user": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_to_user_api_agents__agent_id__to_user_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/to-user/resource": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_resource_api_agents__agent_id__to_user_resource_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/retry-launch": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["retry_launch_api_keyed_v1_agents__agent_id__retry_launch_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/impersonation/force-expire": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_force_expire_impersonation_api_agents__agent_id__impersonation_force_expire_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/terminate": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_agent_terminate_api_agents__agent_id__terminate_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/resurrect": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_agent_resurrect_api_agents__agent_id__resurrect_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/restart": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_agent_restart_api_agents__agent_id__restart_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/native-work": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["native_work_api_keyed_v1_agents__agent_id__native_work_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/cancel-work": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["native_cancel_api_keyed_v1_agents__agent_id__cancel_work_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/restart-work": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["native_restart_api_keyed_v1_agents__agent_id__restart_work_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/restart-commands/{command_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["native_restart_status_api_keyed_v1_agents__agent_id__restart_commands__command_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/compact-target": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["guarded_compact_target_api_keyed_v1_agents__agent_id__compact_target_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/compact-history": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["guarded_compact_history_api_keyed_v1_agents__agent_id__compact_history_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/compact-commands/{command_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["guarded_compact_command_api_keyed_v1_agents__agent_id__compact_commands__command_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/sessions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_sessions_api_agents__agent_id__sessions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/understanding/build": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_understanding_build_api_agents__agent_id__understanding_build_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/understanding/builds/{build_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_understanding_build_api_agents__agent_id__understanding_builds__build_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/messages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_messages_api_agents__agent_id__messages_get"];
        put?: never;
        post: operations["post_agent_message_api_agents__agent_id__messages_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/system-note": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_agent_system_note_api_agents__agent_id__system_note_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/messages/reconcile": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["reconcile_agent_message_api_agents__agent_id__messages_reconcile_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/traces/{trace_id}/messages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_trace_checkpoint_messages_api_agents__agent_id__traces__trace_id__messages_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/last-message": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_last_message_api_agents__agent_id__last_message_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/pending": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_pending_messages_api_agents__agent_id__pending_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/token-usage": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_token_usage_api_agents__agent_id__token_usage_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/context-breakdown": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_context_breakdown_api_agents__agent_id__context_breakdown_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/events/stream": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_events_stream_api_agents__agent_id__events_stream_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_events_api_agents__agent_id__events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/computer/traces": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_computer_trace_api_computer_traces_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/inspect/live": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_inspect_live_api_agents__agent_id__inspect_live_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/inspect/statistics": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_inspect_statistics_api_agents__agent_id__inspect_statistics_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/neighbors": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_neighbors_api_agents__agent_id__neighbors_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/inspect/metrics": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_plugin_metrics_api_agents__agent_id__inspect_metrics_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/inspect/widgets": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_inspect_widgets_api_agents__agent_id__inspect_widgets_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/shell/{session_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_agent_shell_api_agents__agent_id__shell__session_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/timeline": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_timeline_api_agents__agent_id__timeline_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/timeline/retained": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_retained_timeline_api_agents__agent_id__timeline_retained_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/reply-suggestion": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_reply_suggestion_api_agents__agent_id__reply_suggestion_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/system": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_system_broadcast_api_system_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/system": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_system_events_api_agents__agent_id__system_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/system/all": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_all_events_api_system_all_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/host/permissions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_host_permissions_api_host_permissions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/host/permissions/recheck": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["recheck_host_permissions_api_host_permissions_recheck_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/host/permissions/request": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["request_host_permissions_api_host_permissions_request_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/host/permissions/defer": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["defer_host_permissions_api_host_permissions_defer_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/pages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_guarded_page_register_api_keyed_v1_agents__agent_id__pages_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/pages/{name}/close": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_guarded_page_close_api_keyed_v1_agents__agent_id__pages__name__close_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/pages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_all_pages_api_pages_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/pages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_pages_api_agents__agent_id__pages_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/pages/{page_key}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["proxy_page_root_new_pages__page_key__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/pages/{page_key}/{rest}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["proxy_page_get_new_pages__page_key___rest__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/notices/open": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_open_notices_api_notices_open_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/notices/live": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_notices_live_api_notices_live_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/notices": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_notices_feed_api_notices_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/notices": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_notice_create_api_agents__agent_id__notices_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/notices/{notice_id}/resolve": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["resolve_observed_api_keyed_v1_agents__agent_id__notices__notice_id__resolve_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/notices/current/guarded-v1": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch: operations["edit_current_api_agents__agent_id__notices_current_guarded_v1_patch"];
        trace?: never;
    };
    "/api/agents/{agent_id}/notices/current/dismiss/guarded-v1": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["dismiss_current_api_agents__agent_id__notices_current_dismiss_guarded_v1_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cluster/stopping": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_cluster_stopping_api_cluster_stopping_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cluster/status": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_cluster_status_api_cluster_status_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cluster/roster": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_cluster_roster_api_cluster_roster_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cluster/admin/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_cluster_admin_events_api_cluster_admin_events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cluster/machines": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_cluster_machines_api_cluster_machines_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cluster/machines/{name}/staging": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["set_machine_staging_api_cluster_machines__name__staging_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cluster/machines/{name}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete: operations["delete_cluster_machine_api_cluster_machines__name__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cluster/machines/{name}/pause": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["pause_cluster_machine_api_cluster_machines__name__pause_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/cluster/machines/{name}/resume": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["resume_cluster_machine_api_cluster_machines__name__resume_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/commands": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_commands_api_commands_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/config": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_config_api_config_get"];
        put: operations["put_config_api_config_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/config/audit": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_config_audit_api_config_audit_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/config/resolved": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_resolved_config_api_config_resolved_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/conversation-snapshot": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_conversation_snapshot_api_agents__agent_id__conversation_snapshot_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/config/default-model": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_default_model_api_config_default_model_get"];
        put: operations["put_default_model_api_config_default_model_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/settings": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_settings_api_settings_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/settings/{key}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put: operations["put_setting_api_settings__key__put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/schedules": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["list_schedules_api_schedules_get"];
        put?: never;
        post: operations["create_schedule_api_schedules_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/schedules/{schedule_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_schedule_api_schedules__schedule_id__get"];
        put: operations["update_schedule_api_schedules__schedule_id__put"];
        post?: never;
        delete: operations["delete_schedule_api_schedules__schedule_id__delete"];
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/schedules/{schedule_id}/start": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["start_schedule_api_schedules__schedule_id__start_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/schedules/{schedule_id}/stop": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["stop_schedule_api_schedules__schedule_id__stop_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/schedules/{schedule_id}/restart": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["restart_schedule_api_schedules__schedule_id__restart_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/schedules/{schedule_id}/logs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_schedule_logs_api_schedules__schedule_id__logs_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/schedules/{schedule_id}/runs": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_schedule_runs_api_schedules__schedule_id__runs_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/schedules/draft": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["guarded_draft_schedule_api_keyed_v1_schedules_draft_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/guide/draft": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["guarded_draft_guide_api_keyed_v1_guide_draft_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/inventory": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_inventory_api_inventory_get"];
        put: operations["put_inventory_api_inventory_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/skills": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_skills_api_skills_get"];
        put: operations["update_skill_enabled_api_skills_put"];
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/packages/draft": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["guarded_draft_package_api_keyed_v1_packages_draft_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/metrics": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_metrics_api_metrics_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/metrics/agents": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_metrics_agents_api_metrics_agents_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/events": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_events_api_events_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/run-timeline/messages": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_run_timeline_messages_api_agents__agent_id__run_timeline_messages_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/run-timeline/context": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_run_timeline_context_api_agents__agent_id__run_timeline_context_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/run-timeline": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_run_timeline_api_agents__agent_id__run_timeline_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/insights/run-timeline/links": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_run_timeline_links_api_insights_run_timeline_links_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/insights/run-timeline/link-content": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_run_timeline_link_content_api_insights_run_timeline_link_content_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/event-resolutions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["list_event_resolutions_api_event_resolutions_get"];
        put?: never;
        post: operations["create_event_resolution_api_event_resolutions_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/event-resolutions/{dismissal_id}/reopen": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["reopen_event_resolution_api_event_resolutions__dismissal_id__reopen_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/ops/monitor": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_ops_monitor_api_ops_monitor_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/alerts": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["list_alerts_api_alerts_get"];
        put?: never;
        post: operations["ingest_alerts_api_alerts_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/alerts/stream": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_alerts_stream_api_alerts_stream_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/health": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_health_api_health_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/stats/dashboard": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_stats_dashboard_api_stats_dashboard_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/status": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_system_status_api_status_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/stats/alert-classes": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_alert_classes_api_stats_alert_classes_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/stats/alert-classes/samples": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_alert_class_samples_api_stats_alert_classes_samples_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/memory/search": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_memory_search_api_memory_search_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/memory/refresh": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_memory_refresh_api_memory_refresh_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/memory/graph": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_memory_graph_api_memory_graph_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/memory/note": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_memory_note_api_memory_note_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/memory/pool": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_memory_pool_api_memory_pool_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/mcp/clients": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_mcp_clients_api_mcp_clients_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/mcp/clients/{client_id}/revoke": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_mcp_client_revoke_api_mcp_clients__client_id__revoke_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/mcp/clients": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_guarded_mcp_client_api_keyed_v1_mcp_clients_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/fleet/graph": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_fleet_graph_api_fleet_graph_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/frontend-telemetry": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["ingest_api_frontend_telemetry_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/grafana": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["grafana_root_grafana_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/grafana/{rest}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["grafana_proxy_get_grafana__rest__get"];
        put: operations["grafana_proxy_put_grafana__rest__put"];
        post: operations["grafana_proxy_post_grafana__rest__post"];
        delete: operations["grafana_proxy_delete_grafana__rest__delete"];
        options?: never;
        head: operations["grafana_proxy_head_grafana__rest__head"];
        patch: operations["grafana_proxy_patch_grafana__rest__patch"];
        trace?: never;
    };
    "/api/okf/graph": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_okf_graph_api_okf_graph_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/tasks": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_tasks_api_tasks_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/tasks/{task_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch: operations["patch_task_api_tasks__task_id__patch"];
        trace?: never;
    };
    "/api/keyed/v1/task-assignments": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["post_task_assignment_api_keyed_v1_task_assignments_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/plugin-ui/{plugin}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["plugin_ui_root_api_plugin_ui__plugin__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/plugin-ui/{plugin}/{rest}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["plugin_ui_get_api_plugin_ui__plugin___rest__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/ui/contributions": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["get_ui_contributions_api_ui_contributions_get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/uploads": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["upload_files_api_agents__agent_id__uploads_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/agents/{agent_id}/uploads/{filename}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["serve_upload_api_agents__agent_id__uploads__filename__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/uploads": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get?: never;
        put?: never;
        post: operations["upload_api_keyed_v1_agents__agent_id__uploads_post"];
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/uploads/{batch_id}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["status_api_keyed_v1_agents__agent_id__uploads__batch_id__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
    "/api/keyed/v1/agents/{agent_id}/uploads/{batch_id}/objects/{ordinal}": {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        get: operations["object_file_api_keyed_v1_agents__agent_id__uploads__batch_id__objects__ordinal__get"];
        put?: never;
        post?: never;
        delete?: never;
        options?: never;
        head?: never;
        patch?: never;
        trace?: never;
    };
}
export type webhooks = Record<string, never>;
export interface components {
    schemas: {
        Acceptance: {
            batch_id: string;
            agent_id: number;
            files: components["schemas"]["Object"][];
            status_url: string;
        };
        AdmissionOutcome: "admitted" | "maintenance_hold" | "resource_fence" | "admission_guard_refused";
        AgentActivity: {
            active_seconds: number;
            alive_seconds: number;
            active_rate: number;
            llm_seconds: number | null;
            exec_seconds: number;
        };
        AgentAvailability: {
            reason: components["schemas"]["AvailabilityReason"];
            observed_at: string;
            evidence_at?: string | null;
            admission_outcome?: components["schemas"]["AdmissionOutcome"] | null;
        };
        AgentCard: {
            agent_id: number;
            spawner: string;
            fork_source_agent_id: number | null;
            status: components["schemas"]["AgentStatus"];
            pid: number | null;
            spawned_at: string;
            started_at: string | null;
            last_active_at: string;
            last_inbound_at: string;
            label: string | null;
            machine: string;
            supports_vision: boolean | null;
            liveness_state: components["schemas"]["LivenessState"];
            availability?: components["schemas"]["AgentAvailability"] | null;
            observation: components["schemas"]["AgentObservation"];
            awaiting_response_count: number;
            highest_notice_priority: components["schemas"]["Priority"] | null;
            unread_notice_count: number;
            heartbeat_paused_until: string | null;
            open_impersonation_session_id: number | null;
            open_impersonation_status: ("requested" | "accepted" | "active") | null;
        };
        AgentCost: {
            cost_usd: number;
            unpriced_calls: number;
            llm_calls: number;
            tokens_in: number;
            tokens_out: number;
            tokens_cached: number;
            tokens_reasoning: number;
            cache_hit_pct: number | null;
        };
        AgentDirectoryPage: {
            agents: components["schemas"]["AgentCard"][];
            next_cursor: number | null;
        };
        AgentEventRow: {
            id: number;
            line_sha256: string;
            ts: string;
            agent_id: number | null;
            level: string;
            event: string;
            payload: {
                [key: string]: unknown;
            };
        };
        AgentEventsResponse: {
            items: components["schemas"]["AgentEventRow"][];
        };
        AgentInspectLive: {
            agent_id: number;
            machine: string;
            status: string;
            liveness_state: components["schemas"]["LivenessState"];
            last_probe_at?: string | null;
            observation?: components["schemas"]["AgentObservation"] | null;
            shells_available?: boolean | null;
            spawned_at: string;
            started_at?: string | null;
            shells: components["schemas"]["ShellInfo"][];
            config_overlay: {
                [key: string]: unknown;
            };
            notice?: components["schemas"]["OpenNotice"] | null;
            heartbeat: components["schemas"]["HeartbeatInfo"];
        };
        AgentInspectStatistics: {
            agent_id: number;
            window_hours?: components["schemas"]["StatsWindowHours"] | null;
            applied_window_hours?: number | null;
            cost: components["schemas"]["AgentCost"] | null;
            stats: components["schemas"]["AgentStats"] | null;
            tps: components["schemas"]["AgentTps"] | null;
            activity: components["schemas"]["AgentActivity"] | null;
            metadata: components["schemas"]["InspectMetricsMetadata"];
        };
        AgentLineage: {
            agent_id: number;
            spawner: string;
            fork_source_agent_id: number | null;
        };
        AgentMachineRow: {
            name: string;
            description?: string | null;
            live: boolean;
            is_staging: boolean;
        };
        AgentMessageEnqueued: {
            status: components["schemas"]["AgentStatus"];
            inbound_id?: number | null;
        };
        AgentMessageIn: {
            content: string | (components["schemas"]["TextContentBlock"] | components["schemas"]["ImageUrlContentBlock"])[];
            source: string;
            completion_notice: boolean;
        };
        AgentMessagesResponse: {
            messages: {
                [key: string]: unknown;
            }[];
            msg_count: number;
            start_index: number;
            has_more: boolean;
        };
        AgentMetricsItem: {
            agent_id: number;
            label: string | null;
            events: number;
            cost_usd: number;
            llm_calls: number;
            tokens_in: number;
            tokens_out: number;
            tokens_cached: number;
            cache_hit_pct: number | null;
            turn_ok: number;
            turn_total: number;
            exec_ok: number;
            exec_failed: number;
        };
        AgentMetricsReport: {
            meta: components["schemas"]["MetricsMeta"];
            agents: components["schemas"]["AgentMetricsItem"][];
        };
        AgentObservation: {
            machine_probe_at?: string | null;
            machine_probe_valid_until?: string | null;
            runtime_lease_expires_at?: string | null;
            runtime_owner: "unknown";
        };
        AgentRoster: {
            agents: components["schemas"]["AgentCard"][];
            ancestors: components["schemas"]["AgentLineage"][];
        };
        AgentRow: {
            agent_id: number;
            spawner: string;
            fork_source_agent_id: number | null;
            fork_source_checkpoint_id: string | null;
            status: components["schemas"]["AgentStatus"];
            pid: number | null;
            spawned_at: string;
            started_at: string | null;
            last_active_at: string;
            last_inbound_at: string;
            label: string | null;
            machine: string;
            supports_vision: boolean | null;
            liveness_state: components["schemas"]["LivenessState"];
            last_probe_at: string | null;
            last_launch_attempt_id?: string | null;
            availability?: components["schemas"]["AgentAvailability"] | null;
            observation?: components["schemas"]["AgentObservation"] | null;
            notices_awaiting_response: components["schemas"]["OpenNotice"][];
            unread_notice_count: number;
            heartbeat_paused_until: string | null;
        };
        AgentStats: {
            turn_total: number;
            turn_ok: number;
            turn_p50_seconds: number | null;
            turn_p90_seconds: number | null;
            turn_min_seconds: number | null;
            turn_max_seconds: number | null;
            exec_ok: number;
            exec_failed: number;
        };
        AgentStatus: "running" | "idling" | "terminated";
        AgentTps: {
            lm_stage_tps: number | null;
            agent_lifecycle_tps: number | null;
        };
        AlertClassRow: {
            level: "warning" | "error" | "critical";
            event_name: string;
            source: string;
            process: string;
            category: "telemetry" | "log";
            count: number;
            first_seen: string;
            last_seen: string;
            dismissal_id: number | null;
        };
        AlertClassSample: {
            ts: string;
            agent_id: number | null;
            machine: string;
            trace_id: string | null;
            message: string | null;
            attributes: {
                [key: string]: unknown;
            };
        };
        AlertClassSamples: {
            samples: components["schemas"]["AlertClassSample"][];
        };
        AlertClassesResponse: {
            window_hours: components["schemas"]["StatsWindowHours"];
            classes: components["schemas"]["AlertClassRow"][];
            total_classes: number;
            total_events: number;
            as_of: string;
        };
        AlertIngestResult: {
            processed: number;
            inserted: number;
            updated: number;
            notified: number;
        };
        AlertRow: {
            id: number;
            status: "unresolved" | "resolved";
            severity: "critical" | "warning" | "error";
            alertname: string;
            labels: {
                [key: string]: string;
            };
            annotations: {
                [key: string]: string;
            };
            starts_at: string;
            ends_at: string | null;
            fingerprint: string;
            generator_url: string;
            source: string;
            notified_at: string | null;
            created_at: string;
            updated_at: string;
        };
        AlertWebhookAlert: {
            status: "firing" | "resolved";
            labels?: {
                [key: string]: string;
            };
            annotations?: {
                [key: string]: string;
            };
            startsAt: string;
            endsAt: string;
            fingerprint: string;
            generatorURL: string;
            values?: {
                [key: string]: unknown;
            } | null;
        };
        AlertWebhookPayload: {
            source: string;
            status?: ("firing" | "resolved") | null;
            alerts?: components["schemas"]["AlertWebhookAlert"][];
        };
        AlertsListMeta: {
            window: string;
            total: number;
            unresolved_count: number;
        };
        AlertsListResponse: {
            alerts: components["schemas"]["AlertRow"][];
            meta: components["schemas"]["AlertsListMeta"];
        };
        AssignmentAgentIn: {
            label?: string | null;
            machine?: string | null;
            config?: {
                [key: string]: unknown;
            } | null;
        };
        AssignmentTaskIn: {
            title: string;
            description: string;
            parent: number;
            priority: components["schemas"]["Priority"];
            remind_interval_seconds?: number | null;
        };
        AvailabilityReason: "unknown" | "host_unavailable" | "awaiting_admission" | "admission_refused" | "admitted" | "launch_unreachable" | "launch_rejected" | "launch_unknown";
        Body_upload_api_keyed_v1_agents__agent_id__uploads_post: {
            files: string[];
        };
        Body_upload_files_api_agents__agent_id__uploads_post: {
            files: string[];
        };
        BornChainResponse: {
            ancestors: components["schemas"]["BornChainRow"][];
        };
        BornChainRow: {
            agent_id: number;
            label: string | null;
            status: string;
            machine: string | null;
            depth: number;
        };
        BuildJobOut: {
            session: number;
            start_index: number;
            end_index: number;
            first_message: number;
            last_message: number;
            input_tokens: number;
            job_id: number | null;
            state: "planned" | "enqueued" | "merged";
        };
        BuildJobProgress: {
            job_id: number;
            session: number;
            status: "pending" | "running" | "done" | "failed" | "skipped";
            start_index: number;
            end_index: number;
            attempts: number;
            error: string | null;
            calls: number;
            input_tokens: number;
            cache_read_tokens: number;
            output_tokens: number;
            seconds: number;
            cost_usd: number | null;
        };
        BuildProgressResponse: {
            build_id: number;
            agent_id: number;
            created_at: string;
            sessions: number[];
            phase: "chunks" | "rebuild_pending" | "done" | "failed";
            jobs: components["schemas"]["BuildJobProgress"][];
            rebuild: components["schemas"]["RebuildProgressOut"];
            cost_usd: number | null;
        };
        BuildRequest: {
            sessions?: number[] | null;
            from?: string | null;
            to?: string | null;
            dry_run: boolean;
        };
        BuildResponse: {
            agent_id: number;
            dry_run: boolean;
            understanding_enabled: boolean;
            sessions: number[];
            jobs: components["schemas"]["BuildJobOut"][];
            queued: number;
            merged: number;
            estimate: components["schemas"]["CostEstimateOut"];
            cost_basis: string;
            build_id: number | null;
            rebuild_id: number | null;
        };
        ChoiceQuestion: {
            type: "multi_select" | "single_select";
            options: components["schemas"]["NoticeOption"][];
            allow_other: boolean;
        };
        ClusterPanel: {
            current_machine: string;
            current_serve_gateway: boolean;
            current_serve_agent_runner: boolean;
            current_serve_observability_station: boolean;
            current_paused: boolean;
            machines: components["schemas"]["MachineStatus"][];
        };
        ClusterStatus: {
            machine_name: string;
            serve_gateway: boolean;
            serve_agent_runner: boolean;
            serve_observability_station: boolean;
            paused: boolean;
            paused_reason?: ("no_state" | "business_pause" | "maintenance" | "startup") | null;
            head_sha?: string | null;
            running_sha?: string | null;
            schema_mismatch?: components["schemas"]["SchemaMismatchStatus"] | null;
            shell_count: number;
            agent_host_online?: boolean | null;
            supervisor_online?: boolean | null;
            agent_count: number;
            session_count: number;
            agent_groups: {
                [key: string]: unknown;
            }[];
            resource?: components["schemas"]["ResourceSample"] | null;
        };
        CommandItem: {
            name: string;
            description: string;
            instruction_hint: string;
        };
        CompactAcceptance: {
            command_id: string;
            target: components["schemas"]["CompactTarget"];
        };
        CompactOutcome: "accepted" | "prepared" | "applying" | "applied" | "noop" | "rejected" | "uncertain";
        CompactStatus: {
            acceptance: components["schemas"]["CompactAcceptance"];
            outcome: components["schemas"]["CompactOutcome"];
            reason: string | null;
            checkpoint_id: string | null;
            recovery_checkpoint_id: string | null;
            attempt_id: string | null;
            attempt_provider: string | null;
            execution: components["schemas"]["NativeWorkTarget"] | null;
            result_available: boolean;
            continuation_released: boolean;
        };
        CompactTarget: {
            protocol: number;
            observation_id: string;
            source: components["schemas"]["NativeWorkTarget"];
            checkpoint_id: string;
            checkpoint_ns: string;
            messages_version: string;
            compact_channel_version: string | null;
            segment_version: number;
            model: string;
        };
        ConfigAuditView: {
            records: {
                [key: string]: unknown;
            }[];
        };
        ConfigFieldView: {
            name: string;
            owner?: string | null;
            field_type: string;
            current_value: unknown;
            default_value: unknown;
            description: string;
            group: string;
            capability: "gateway" | "agent-runner" | "common";
            restart_required: string;
            writable: boolean;
            sensitive: boolean;
            env_var: string;
            scope: string;
            per_agent: boolean;
            remote_writable: boolean;
            choices?: string[] | null;
            can_enable?: boolean | null;
            reason?: string | null;
        };
        ConfigFieldWriteResult: {
            ok: boolean;
            reason?: string | null;
        };
        ConfigNormalization: {
            requested: string;
            resolved: string;
        };
        ConfigView: {
            fields: components["schemas"]["ConfigFieldView"][];
            raw_overrides: {
                [key: string]: unknown;
            };
            machine_capabilities: ("gateway" | "agent-runner" | "observability-station")[];
        };
        ConfigWriteResult: {
            applied: boolean;
            results: {
                [key: string]: components["schemas"]["ConfigFieldWriteResult"];
            };
            restart_required: string[];
        };
        ContextBreakdownResponse: {
            total_input_tokens: number;
            estimated: boolean;
            exact_fraction: number;
            max_input_tokens: number;
            soft_compact_tokens: number;
            hard_compact_tokens: number;
            sections: components["schemas"]["ContextSection"][];
            categories: components["schemas"]["ContextCategory"][];
        };
        ContextCategory: {
            kind: string;
            tokens: number;
            estimated: boolean;
            exact_fraction: number;
        };
        ContextSection: {
            name: string;
            tokens: number;
            estimated: boolean;
            children?: components["schemas"]["ContextSection"][];
        };
        ConversationSnapshotResponse: {
            timeline: components["schemas"]["TimelineResponse"];
            token_usage: components["schemas"]["TokenUsageResponse"];
            pending: components["schemas"]["PendingInbound"][];
        };
        CostEstimateOut: {
            jobs: number;
            input_tokens: number;
            output_tokens: number;
            cost_usd: number | null;
        };
        CredentialCreationRequest: {
            name: string;
            scope: components["schemas"]["McpClientScope"];
        };
        CredentialCreationResult: {
            id: number;
            name: string;
            scope: components["schemas"]["McpClientScope"];
            created_at: string;
            replayed: boolean;
            token: string | null;
        };
        DefaultModelSource: "cluster" | "config";
        DefaultModelView: {
            model: string;
            source: components["schemas"]["DefaultModelSource"];
        };
        DefaultModelWrite: {
            model: string;
        };
        DeliveryStatus: {
            acceptance: components["schemas"]["Acceptance"] | null;
            state: "receiving" | "pending" | "accepted" | "hold";
            reason: string | null;
            inbound_id: number | null;
            source: components["schemas"]["UnitIdentity"];
            target: components["schemas"]["UnitIdentity"];
            attempts: number;
            next_attempt_at: string | null;
        };
        EventResolutionCreate: {
            category: "telemetry" | "log";
            level: "warning" | "error" | "critical";
            event_name: string;
            source: string;
            process: string;
            agent_id?: number | null;
            note: string;
        };
        EventResolutionListResponse: {
            resolutions: components["schemas"]["EventResolutionRow"][];
        };
        EventResolutionRow: {
            id: number;
            category: "telemetry" | "log";
            level: "warning" | "error" | "critical";
            event_name: string;
            source: string;
            process: string;
            agent_id: number | null;
            dismissed_by: number;
            note: string;
            status: "dismissed" | "reopened";
            dismissed_at: string;
            reopened_at: string | null;
            burst_count: number | null;
            created_at: string;
            updated_at: string;
        };
        EventRow: {
            id: number;
            line_sha256: string;
            ts: string;
            trace_id: string | null;
            span_id: string | null;
            agent_id: number | null;
            machine: string;
            process: string;
            category: string;
            event_name: string;
            tier: "business" | "anomaly" | "observation" | "noise";
            level: string;
            source: string;
            target_agent_id: number | null;
            attributes: {
                [key: string]: unknown;
            };
        };
        EventsMeta: {
            total: number | null;
            window_from: string | null;
            window_to: string | null;
            limit: number;
            offset: number;
            has_more: boolean;
            generated_at: string;
        };
        EventsResponse: {
            meta: components["schemas"]["EventsMeta"];
            items: components["schemas"]["EventRow"][];
        };
        FleetGraphEdge: {
            from_agent: number;
            to_agent: number;
            event_type: string;
            weight: number;
            event_count: number;
            last_seen_at: string;
        };
        FleetGraphNode: {
            agent_id: number;
            label: string | null;
            status: components["schemas"]["AgentStatus"];
            liveness_state: components["schemas"]["LivenessState"];
            spawner: string;
            machine: string | null;
            node_score: number;
            total_tokens: number;
        };
        FleetGraphResponse: {
            nodes: components["schemas"]["FleetGraphNode"][];
            edges: components["schemas"]["FleetGraphEdge"][];
            telemetry_stale: boolean;
            snapshot_at?: string | null;
        };
        ForceExpireImpersonationRequest: {
            session_id: number;
        };
        ForceExpireImpersonationResponse: {
            session_id: number;
            status: "expired" | "not_open";
        };
        GuardedNoticeEdit: {
            observed_notice_id: number;
            title?: string | null;
            content?: string | null;
            priority?: components["schemas"]["Priority"] | null;
            blocking?: boolean | null;
        };
        GuideDraftRequest: {
            nl: string;
        };
        GuideDraftResponse: {
            agent_id: number;
        };
        HTTPValidationError: {
            detail?: components["schemas"]["ValidationError"][];
        };
        HeartbeatInfo: {
            interval_s: number;
            next_at?: string | null;
            paused_until?: string | null;
            heartbeat_pending: boolean;
            last_pause?: components["schemas"]["HeartbeatLastPause"] | null;
        };
        HeartbeatLastPause: {
            at: string;
            duration_s: number;
        };
        HostPermissionsPayload: {
            state: string;
            headline: string;
            checked_at: string;
            diagnostic: string;
            helper_reachable: boolean;
            required_total: number;
            required_missing: number;
            items: components["schemas"]["PermissionItemRow"][];
        };
        ImageUrlContentBlock: {
            type: "image_url";
            image_url: components["schemas"]["ImageUrlRef"];
        };
        ImageUrlRef: {
            url: string;
        };
        ImpersonationMetadata: {
            agent_id: number;
            session_id: number;
            name: string;
            executor_name: string;
            provider: string | null;
            anchor_item_id?: string | null;
            seq?: number | null;
        };
        InspectMetricsMetadata: {
            collection: "observed";
            window_start: string | null;
            window_end: string;
            sampled_at: string;
            collection_started_at: string;
            last_observed_at: string | null;
            cost: components["schemas"]["MetricEvidence"];
            turns: components["schemas"]["MetricEvidence"];
            activity: components["schemas"]["MetricEvidence"];
            lifecycle: components["schemas"]["MetricEvidence"];
        };
        InspectWidgetResult: {
            plugin: string;
            id: string;
            kind: "taskList";
            order: number;
            title?: string | null;
            tasks?: components["schemas"]["InspectWidgetTask"][];
        };
        InspectWidgetTask: {
            id: number;
            title: string;
            priority: components["schemas"]["Priority"];
        };
        InventoryAggregate: {
            machines: string[];
            unreachable: string[];
            plugins: components["schemas"]["InventoryItemAggregate"][];
            mcp_servers: components["schemas"]["InventoryItemAggregate"][];
        };
        InventoryItem: {
            name: string;
            kind: string;
            enabled: boolean;
            can_enable?: boolean | null;
            reason?: string | null;
            description: string;
        };
        InventoryItemAggregate: {
            name: string;
            kind: string;
            description: string;
            hosts: {
                [key: string]: components["schemas"]["InventoryItemHostState"];
            };
        };
        InventoryItemHostState: {
            present: boolean;
            enabled: boolean;
            can_enable?: boolean | null;
            reason?: string | null;
        };
        InventoryItemWriteResult: {
            ok: boolean;
            reason?: string | null;
        };
        InventoryMachineView: {
            machine: string;
            plugins: components["schemas"]["InventoryItem"][];
            mcp_servers: components["schemas"]["InventoryItem"][];
        };
        InventoryWriteRequest: {
            plugins: {
                [key: string]: boolean;
            };
            mcp_servers: {
                [key: string]: boolean;
            };
        };
        InventoryWriteResult: {
            applied: boolean;
            plugin_results: {
                [key: string]: components["schemas"]["InventoryItemWriteResult"];
            };
            mcp_results: {
                [key: string]: components["schemas"]["InventoryItemWriteResult"];
            };
        };
        LabelPatchRequest: {
            label: string;
            source: "user" | "self";
        };
        LastMessageResponse: {
            text: string | null;
        };
        LivenessState: "online" | "offline" | "unknown";
        LlmBucket: {
            bucket: number;
            calls: number;
            latency_p50_ms: number | null;
            latency_p95_ms: number | null;
            latency_max_ms: number | null;
            tokens_in: number;
            tokens_out: number;
            tps: number | null;
            errors: number;
        };
        LlmReport: {
            series: components["schemas"]["LlmBucket"][];
            totals: components["schemas"]["LlmTotals"];
        };
        LlmTotals: {
            calls: number;
            latency_p50_ms: number | null;
            latency_p95_ms: number | null;
            latency_max_ms: number | null;
            tokens_in: number;
            tokens_out: number;
            tps: number | null;
            errors: number;
        };
        LoginRequest: {
            password: string;
            username?: string | null;
        };
        LoginTicketRedeemRequest: {
            ticket: string;
        };
        MachineDeleteResponse: {
            deleted: boolean;
        };
        MachinePauseRequest: {
            reason: string;
        };
        MachinePauseResponse: {
            name: string;
            paused: boolean;
            terminated_agents: number;
            force_marked_agents: number;
            reassigned_tasks: number;
            paused_at?: string | null;
            pause_reason?: string | null;
        };
        MachineResumeResponse: {
            name: string;
            resumed: boolean;
        };
        MachineStagingRequest: {
            is_staging: boolean;
        };
        MachineStatus: {
            name: string;
            serve_gateway: boolean;
            serve_agent_runner: boolean;
            serve_observability_station: boolean;
            gateway_url: string;
            up_since_at: string;
            online: boolean;
            paused: boolean | null;
            paused_reason?: ("no_state" | "business_pause" | "maintenance" | "startup") | null;
            description?: string | null;
            stopped_at?: string | null;
            is_staging: boolean;
            head_sha?: string | null;
            running_sha?: string | null;
            schema_mismatch?: components["schemas"]["SchemaMismatchStatus"] | null;
            identity_mismatch: boolean;
            observed_at?: string | null;
            shell_count: number;
            agent_host_online?: boolean | null;
            supervisor_online?: boolean | null;
            agent_count: number;
            session_count: number;
            agent_groups: {
                [key: string]: unknown;
            }[];
            resource?: components["schemas"]["ResourceSample"] | null;
        };
        McpClientRevoked: {
            ok: boolean;
        };
        McpClientScope: "read" | "write";
        McpClientView: {
            id: number;
            name: string;
            scope: components["schemas"]["McpClientScope"];
            created_at: string;
            revoked_at: string | null;
            last_used_at: string | null;
        };
        MemoryGraphEdge: {
            source: string;
            target: string;
            kind: "containment" | "reference";
        };
        MemoryGraphNode: {
            id: string;
            path: string;
            title: string;
            kind: "note" | "folder";
            description: string | null;
            tags: string[];
            primary_tag: string;
            timestamp: string | null;
            meiva_agent: string | null;
            meiva_machine: string | null;
        };
        MemoryGraphResponse: {
            nodes: components["schemas"]["MemoryGraphNode"][];
            edges: components["schemas"]["MemoryGraphEdge"][];
            warnings?: string[];
        };
        MemoryNoteResponse: {
            path: string;
            title: string;
            description: string | null;
            tags: string[];
            timestamp: string | null;
            meiva_agent: string | null;
            meiva_machine: string | null;
            body: string;
        };
        MemoryRefreshResponse: {
            head: string;
        };
        MemorySearchRequest: {
            query: string;
            k: number;
            purpose: "recall" | "search" | "dedup";
            exclude?: string[];
            deadline_seconds?: number | null;
        };
        MemorySearchResponse: {
            results: components["schemas"]["MemorySearchResultItem"][];
        };
        MemorySearchResultItem: {
            path: string;
            description: string;
            tags?: string[];
            score: number;
            chunk: string;
        };
        MetricEvidence: {
            availability: "observed" | "partial" | "unavailable";
            sources: string[];
            retained_unapplied_sources?: string[];
            reason?: ("historical_coverage_unknown" | "missing_turn_durations" | "archive_precision_unattributed") | null;
            duration_precision?: ("exact" | "one_second_buckets" | "mixed") | null;
        };
        MetricPoint: {
            ts: string;
            value: number;
        };
        MetricsMeta: {
            window_days: number;
            agent_filter: number | null;
            generated_at: string;
            total_events: number;
            distinct_agents: number;
            since_compact: boolean;
        };
        MetricsReport: {
            meta: components["schemas"]["MetricsMeta"];
            metrics: {
                [key: string]: unknown;
            };
        };
        ModelInfo: {
            provider: string;
            context_window: number;
            pricing?: components["schemas"]["ModelPricing"] | null;
            reference_tps?: components["schemas"]["ReferenceTps"] | null;
            fast_of?: string | null;
            reasoning_effort_options?: string[] | null;
            reasoning_effort_default?: string | null;
            superseded_by?: string | null;
        };
        ModelPricing: {
            input: number;
            cache_read: number;
            output: number;
            cache_write_5m?: number | null;
            cache_write_1h?: number | null;
        };
        ModelsResponse: {
            providers: {
                [key: string]: string[];
            };
            models: {
                [key: string]: components["schemas"]["ModelInfo"];
            };
            default: string;
        };
        NativeCancelAcceptance: {
            command_id: string;
            target: components["schemas"]["NativeWorkTarget"];
        };
        NativeRestartAcceptance: {
            command_id: number;
            target: components["schemas"]["NativeWorkTarget"];
            config_overlay: {
                [key: string]: unknown;
            } | null;
        };
        NativeRestartOutcome: "accepted" | "applied" | "observed" | "superseded" | "uncertain";
        NativeRestartProgress: {
            acceptance: components["schemas"]["NativeRestartAcceptance"];
            outcome: components["schemas"]["NativeRestartOutcome"];
            applied_at: string | null;
            observed_at: string | null;
            reason: components["schemas"]["NativeRestartReason"] | null;
        };
        NativeRestartReason: "target_replaced" | "resurrect" | "force_terminate" | "invalid_source_transition" | "source_command_unavailable";
        NativeRestartRequest: {
            target: components["schemas"]["NativeWorkTarget"];
            source: string;
            config_overlay?: {
                [key: string]: unknown;
            } | null;
        };
        NativeWorkTarget: {
            work_id: string;
            agent_id: number;
            machine: string;
            generation: string;
            owner: string;
            protocol: 1;
        };
        NeighborRow: {
            agent_id: number;
            label: string | null;
            status: string;
            depth: number;
            score: number;
        };
        NeighborsResponse: {
            neighbors: components["schemas"]["NeighborRow"][];
            ancestors: components["schemas"]["NeighborRow"][];
        };
        NoticeAnswer: {
            option_ids?: string[];
            other_text?: string | null;
            text?: string | null;
            comment?: string | null;
        };
        NoticeCreateIn: {
            title: string;
            content?: string | null;
            priority: components["schemas"]["Priority"];
            require_response: boolean;
            blocking: boolean;
            task_id?: number | null;
            expire_at?: string | null;
            question?: (components["schemas"]["ChoiceQuestion"] | components["schemas"]["TextQuestion"]) | null;
        };
        NoticeItem: {
            id: number;
            agent_id: number;
            agent_label: string | null;
            title: string;
            content: string | null;
            priority: components["schemas"]["Priority"];
            require_response: boolean;
            blocking: boolean;
            created_at: string;
            updated_at?: string | null;
            resolved_at?: string | null;
            resolution?: string | null;
            reply?: string | null;
            question?: (components["schemas"]["ChoiceQuestion"] | components["schemas"]["TextQuestion"]) | null;
            answer?: components["schemas"]["NoticeAnswer"] | null;
            task_id?: number | null;
            expire_at: string;
        };
        NoticeOption: {
            id: string;
            label: string;
            description?: string | null;
        };
        NoticesCursor: {
            before_at: string;
            before_id: number;
        };
        NoticesFeed: {
            open: components["schemas"]["NoticeItem"][];
            awaiting: components["schemas"]["NoticeItem"][];
            resolved_page: components["schemas"]["NoticeItem"][];
            next_cursor: components["schemas"]["NoticesCursor"] | null;
        };
        Object: {
            ordinal: number;
            filename: string;
            name: string;
            size: number;
            sha256: string;
            content_type: string;
        };
        ObservedNotice: {
            observed_notice_id: number;
        };
        OpenNotice: {
            id: number;
            title: string;
            content: string | null;
            priority: components["schemas"]["Priority"];
            require_response: boolean;
            blocking: boolean;
            created_at: string;
            expire_at?: string | null;
            question?: (components["schemas"]["ChoiceQuestion"] | components["schemas"]["TextQuestion"]) | null;
            task_id?: number | null;
        };
        OpenTaskRow: {
            id: number;
            title: string;
            status: "in_progress";
            updated_at: string;
        };
        OpenTasksHint: {
            count: number;
            tasks: components["schemas"]["OpenTaskRow"][];
            more: number;
        };
        OpsMonitorMeta: {
            window: string;
            bucket_seconds: number;
            generated_at: string;
            bucket_starts: string[];
        };
        OpsMonitorReport: {
            meta: components["schemas"]["OpsMonitorMeta"];
            sse: components["schemas"]["SseReport"];
            llm: components["schemas"]["LlmReport"];
            restarts: components["schemas"]["RestartReport"];
        };
        PackageDraftRequest: {
            kind: "skill" | "plugin" | "mcp";
            nl: string;
        };
        PackageDraftResponse: {
            agent_id: number;
        };
        PageCloseRequest: {
            expected_page_id: number;
        };
        PageRegisterRequest: {
            name: string;
            host: string;
            port: number;
            title?: string | null;
            serve_dir?: string | null;
            ttl_seconds?: number | null;
        };
        PageRow: {
            id: number;
            agent_id: number;
            name: string;
            port: number;
            title: string | null;
            serve_dir: string | null;
            url: string;
            created_at: string;
            closed_at: string | null;
        };
        PendingInbound: {
            id: number;
            source: string | null;
            content: string;
            images: string[] | null;
            created_at: string;
        };
        PermissionItemRow: {
            id: string;
            title: string;
            group: string;
            state: string;
            state_label: string;
            mode: string;
            why: string;
            jump: string;
            verify: string;
            restart: string;
            required: boolean;
            blocking: boolean;
            detail: string;
        };
        PluginMetricResult: {
            name: string;
            title: string;
            description: string;
            plugin: string;
            unit: string;
            panel: "timeseries" | "stat" | "barchart" | "table" | "logs";
            error?: string | null;
            value?: number | null;
            series?: components["schemas"]["MetricPoint"][];
        };
        PluginStat: {
            plugin: string;
            id: string;
            value: string;
            detail: string | null;
            status: "ok" | "warn" | "error";
            updated_at: string;
            updated_by: string | null;
        };
        Priority: "P0" | "P1" | "P2" | "P3";
        RebuildProgressOut: {
            id: number;
            status: "pending" | "running" | "done" | "failed";
            attempts: number;
            error: string | null;
            leaves: number;
            calls: number;
            input_tokens: number;
            cache_read_tokens: number;
            output_tokens: number;
            seconds: number;
            cost_usd: number | null;
            levels: {
                [key: string]: number;
            };
        };
        ReferenceTps: {
            display: string;
            source_url: string;
            source_checked_at: string;
            note: string;
        };
        ReplySuggestionResponse: {
            suggestions: string[];
            reason?: ("disabled" | "agent_not_idle" | "no_agent_text" | "not_useful" | "gate_failed" | "generation_failed" | "invalid_generation" | "client_disconnected") | null;
        };
        ResolveNoticeIn: {
            action: "answer" | "dismiss" | "read";
            reply?: string | null;
            answer?: components["schemas"]["NoticeAnswer"] | null;
        };
        ResolvedConfigView: {
            model: string;
            registered: boolean;
            fields: components["schemas"]["ResolvedFieldView"][];
        };
        ResolvedFieldView: {
            name: string;
            env_var: string;
            description: string;
            field_type: string;
            choices?: string[] | null;
            group: string;
            effective_value: unknown;
            source: "explicit" | "model-default" | "shared-default";
            explicit_value: unknown;
            model_default: unknown;
            shared_default: unknown;
            per_agent: boolean;
            restart_required: string;
        };
        ResourceSample: {
            ts: number;
            cpu_pct: number;
            mem_used_gb: number;
            mem_total_gb: number;
            mem_pct: number;
            disk_used_gb: number;
            disk_total_gb: number;
            disk_pct: number;
            battery_percent?: number | null;
            battery_power?: ("ac" | "battery") | null;
            battery_charging?: boolean | null;
            battery_remaining_min?: number | null;
        };
        RestartAgentRequest: {
            source: string;
            config_overlay?: {
                [key: string]: unknown;
            } | null;
        };
        RestartAgentResponse: {
            status: components["schemas"]["RestartResult"];
        };
        RestartBucket: {
            bucket: number;
            service_starts: number;
        };
        RestartReport: {
            series: components["schemas"]["RestartBucket"][];
            services: components["schemas"]["ServiceRestartRow"][];
            totals: components["schemas"]["RestartTotals"];
        };
        RestartResult: "enqueued" | "already_terminated";
        RestartTotals: {
            service_starts: number;
        };
        ResurrectAgentRequest: {
            resurrected_by: string;
            prompt?: string | null;
        };
        ResurrectAgentResponse: {
            status: components["schemas"]["ResurrectResult"];
        };
        ResurrectResult: "spawned" | "already_alive";
        RetainedTimelineResponse: {
            boundary_checkpoint_id: string | null;
            items: components["schemas"]["TimelineItem"][];
            has_more: boolean;
        };
        RetryLaunchAccepted: {
            agent_id: number;
            prior_attempt_id: string;
            launch_attempt_id: string;
            accepted_at: string;
            accepted: true;
            execution_observed: false;
        };
        RetryLaunchRequest: {
            expected_prior_attempt_id: string;
        };
        RunTimelineContext: {
            total_input_tokens: number;
            estimated: boolean;
            exact_fraction: number;
            max_input_tokens: number;
            soft_compact_tokens: number;
            hard_compact_tokens: number;
            sections: components["schemas"]["ContextSection"][];
            categories: components["schemas"]["ContextCategory"][];
            request: number;
            session: number;
            sessions: number;
            ts: string;
        };
        RunTimelineGeneration: {
            calls: number;
            input: number;
            cache_read: number;
            output: number;
            seconds: number;
        };
        RunTimelineLink: {
            kind: "send_message" | "spawn" | "fork" | "terminate" | "restart" | "resurrect" | "notice";
            ts: string;
            sender: number | null;
            receiver: number | null;
            inbound_id: number | null;
            fork_from: number | null;
            notice_id: number | null;
        };
        RunTimelineLinkContent: {
            title: string | null;
            content: string;
        };
        RunTimelineLinks: {
            links: components["schemas"]["RunTimelineLink"][];
        };
        RunTimelineMessage: {
            idx: number;
            ts: string | null;
            source: string | null;
            parts: components["schemas"]["RunTimelineMessagePart"][];
            context_tokens: number | null;
            estimated: boolean | null;
        };
        RunTimelineMessagePart: {
            kind: "think" | "text" | "call" | "out" | "note" | "compact" | "inbound" | "attach" | "prompt";
            chars: number;
            text: string;
            text_truncated: boolean;
        };
        RunTimelineMessages: {
            messages: components["schemas"]["RunTimelineMessage"][];
            next_start: number | null;
        };
        RunTimelineNode: {
            id: string;
            level: number;
            parent: string | null;
            start: string;
            end: string;
            span_start: number;
            span_end: number;
            summary: string;
            usage: components["schemas"]["RunTimelineUsage"];
            generation: components["schemas"]["RunTimelineGeneration"] | null;
            context_tokens: number | null;
            estimated: boolean | null;
        };
        RunTimelineResponse: {
            agent_id: number;
            window: components["schemas"]["RunTimelineWindow"];
            lifetime: components["schemas"]["RunTimelineWindow"] | null;
            nodes: components["schemas"]["RunTimelineNode"][];
            units: components["schemas"]["RunTimelineUnit"][];
            units_omitted: number;
            snapshot?: string | null;
        };
        RunTimelineUnit: {
            kind: "inbound" | "text" | "note" | "thinking" | "call" | "output";
            i0: number;
            i1: number;
            start: string;
            end: string;
            source: string | null;
            inbound_id: number | null;
            preview: string;
            parent: string | null;
            context_tokens: number | null;
            generation_tokens: number | null;
            estimated: boolean | null;
            session: number;
            context_total: number | null;
            request: components["schemas"]["RunTimelineUsage"] | null;
        };
        RunTimelineUsage: {
            calls: number;
            input: number;
            cache_read: number;
            output: number;
            cache_write: number;
            cost_usd: number;
            cost_calls: number;
        };
        RunTimelineWindow: {
            from: string;
            to: string;
        };
        ScheduleCreate: {
            name: string;
            script: string;
            command: string;
            description?: string | null;
            enabled: boolean;
        };
        ScheduleDraftRequest: {
            nl: string;
        };
        ScheduleDraftResponse: {
            agent_id: number;
        };
        ScheduleLogsView: {
            source: string;
            lines: string[];
        };
        ScheduleRunView: {
            id: number;
            ran_at: string;
            ok: boolean | null;
            agent_id: number | null;
            note: string | null;
        };
        ScheduleSummary: {
            id: number;
            name: string;
            description: string | null;
            command: string;
            enabled: boolean;
            status: string;
            last_error: string | null;
            created_at: string;
            updated_at: string;
        };
        ScheduleUpdate: {
            name?: string | null;
            script?: string | null;
            command?: string | null;
            description?: string | null;
            enabled?: boolean | null;
        };
        ScheduleView: {
            id: number;
            name: string;
            description: string | null;
            command: string;
            enabled: boolean;
            status: string;
            last_error: string | null;
            created_at: string;
            updated_at: string;
            script: string;
        };
        SchemaMismatchStatus: {
            kind: "schema-ahead-of-code" | "schema-behind-code" | "divergent" | "invalid-migration-layout" | "unavailable";
            machine: string;
            detail: string;
        };
        SdkCall: {
            method: string;
            count: number;
        };
        ServiceItem: {
            name: string;
            label: string;
            online: boolean | null;
            pid: number | null;
            detail: string | null;
        };
        ServiceRestartRow: {
            name: string;
            starts: number;
            last_start: string | null;
        };
        ServicesStatus: {
            items: components["schemas"]["ServiceItem"][];
        };
        SessionCoverage: {
            status: "none" | "partial" | "full";
            ratio: number;
            covered_messages: number;
            total_messages: number;
        };
        SessionOut: {
            number: number;
            boundary_checkpoint_id: string | null;
            start: string | null;
            end: string | null;
            messages: number;
            peak_input_tokens: number;
            context_tokens: number;
            generation_tokens: number;
            estimated: boolean;
            exact_fraction: number;
            coverage: components["schemas"]["SessionCoverage"];
            estimate: components["schemas"]["CostEstimateOut"];
        };
        SessionsResponse: {
            agent_id: number;
            model: string;
            understanding_enabled: boolean;
            cost_basis: string;
            sessions: components["schemas"]["SessionOut"][];
        };
        ShellCaptureResponse: {
            agent_id: number;
            session_id: number;
            session_name: string;
            lines: string[];
            created_at?: string | null;
            uptime_seconds: number;
            expires_at?: string | null;
            renewals: number;
            last_renewed_at?: string | null;
        };
        ShellInfo: {
            id: number;
            name?: string | null;
            created_at?: string | null;
            uptime_seconds: number;
            expires_at?: string | null;
        };
        ShellSessionKillTiming: "now" | "at_exit";
        ShellSessionsKill: {
            when: components["schemas"]["ShellSessionKillTiming"];
            killed?: number[];
        };
        SkillEnableUpdate: {
            name: string;
            enabled: boolean;
        };
        SkillView: {
            name: string;
            layer: "core" | "plugin" | "machine" | "untracked";
            enabled: boolean;
            modified_locally: boolean;
            origin_path?: string | null;
        };
        SkillsView: {
            skills: components["schemas"]["SkillView"][];
        };
        SpawnAgentRequest: {
            prompt?: string | null;
            spawner: string;
            fork_from?: number | null;
            prompt_source?: string | null;
            machine?: string | null;
            config?: {
                [key: string]: unknown;
            } | null;
            label?: string | null;
        };
        SpawnedAgent: {
            id: number;
            config_normalized?: components["schemas"]["ConfigNormalization"] | null;
            accepted?: boolean | null;
            execution_observed?: boolean | null;
            reason?: components["schemas"]["AvailabilityReason"] | null;
            observed_at?: string | null;
        };
        SseDropBucket: {
            bucket: number;
            queue_full: number;
            publish_error: number;
            event_log_drop: number;
        };
        SseReport: {
            series: components["schemas"]["SseDropBucket"][];
            totals: components["schemas"]["SseTotals"];
        };
        SseTotals: {
            queue_full: number;
            publish_error: number;
            event_log_drop: number;
        };
        StatsDashboard: {
            live_count: number;
            window_hours: components["schemas"]["StatsWindowHours"];
            applied_window_hours?: number | null;
            tokens: components["schemas"]["StatsTokens"];
            cost_usd: number;
            avg_turn_seconds: number | null;
            warnings: number;
            errors: number;
            alert_classes_active: number;
            alert_classes_dismissed: number;
            plugin_stats: components["schemas"]["PluginStat"][];
            as_of?: string | null;
        };
        StatsTokens: {
            input: number;
            output: number;
            cache_read: number;
            cache_hit_pct: number | null;
        };
        StatsWindowHours: 0 | 1 | 6 | 24 | 72 | 168;
        SystemNoteIn: {
            content: string;
            source: string;
            note_tag: string;
            task_id?: number | null;
            resurrect: boolean;
        };
        SystemStatus: {
            services: components["schemas"]["ServicesStatus"];
            cluster: components["schemas"]["ClusterPanel"];
        };
        Task: {
            id: number;
            parent_id: number | null;
            title: string;
            description: string;
            results: string | null;
            status: string;
            owner: number | null;
            created_by: string;
            created_at: string;
            updated_at: string;
            remind_interval_seconds?: number | null;
            last_reminded_at?: string | null;
            reminder_count: number;
            priority: string;
        };
        TaskAssignmentAccepted: {
            task: components["schemas"]["Task"];
            agent_id: number;
            launch_attempt_id: string;
            launch?: components["schemas"]["SpawnedAgent"] | null;
            launch_failure?: string | null;
            retry_launch_path?: string | null;
        };
        TaskAssignmentIn: {
            actor_agent_id: number;
            task: components["schemas"]["AssignmentTaskIn"];
            agent: components["schemas"]["AssignmentAgentIn"];
        };
        TaskListResponse: {
            tasks: (components["schemas"]["TaskRow"] | components["schemas"]["TaskSummaryRow"])[];
        };
        TaskRow: {
            id: number;
            parent_id: number | null;
            title: string;
            description: string;
            results: string | null;
            status: components["schemas"]["TaskStatus"];
            priority: components["schemas"]["Priority"];
            owner: number | null;
            owner_label?: string | null;
            created_by: string;
            created_at: string;
            updated_at: string;
            remind_interval_seconds?: number | null;
            last_reminded_at?: string | null;
            reminder_count: number;
            ghost: boolean;
        };
        TaskStatus: "in_progress" | "done" | "cancelled";
        TaskSummaryRow: {
            id: number;
            parent_id: number | null;
            title: string;
            status: components["schemas"]["TaskStatus"];
            owner: number | null;
            owner_label?: string | null;
            created_by: string;
            created_at: string;
            updated_at: string;
            remind_interval_seconds?: number | null;
            last_reminded_at?: string | null;
            reminder_count: number;
            priority: components["schemas"]["Priority"];
            ghost: boolean;
        };
        TaskUpdateRequest: {
            status?: components["schemas"]["TaskStatus"] | null;
            title?: string | null;
            description?: string | null;
            results?: string | null;
            remind_interval_seconds?: number | null;
            owner?: number | null;
            priority?: components["schemas"]["Priority"] | null;
            parent_id?: number | null;
        };
        TerminateAgentRequest: {
            force: boolean;
            kill_all_shell_sessions: boolean;
            source: string;
            message?: string | null;
        };
        TerminateAgentResponse: {
            status: components["schemas"]["TerminateResult"];
            open_tasks?: components["schemas"]["OpenTasksHint"] | null;
            shell_sessions?: components["schemas"]["ShellSessionsKill"] | null;
        };
        TerminateResult: "enqueued" | "already_terminated";
        TextContentBlock: {
            type: "text";
            text: string;
        };
        TextQuestion: {
            type: "text";
        };
        TimelineItem: {
            item_id: string;
            kind: "inbound_chat" | "inbound_compact_summary" | "inbound_compact_request" | "attach" | "agent_chat" | "agent_code" | "agent_reasoning" | "code_output" | "system_prompt" | "system_marker";
            source?: string | null;
            payload: string;
            created_at?: string | null;
            impersonation?: components["schemas"]["ImpersonationMetadata"] | null;
            inbound_id?: number | null;
            source_message_id?: string | null;
            source_inbound_id?: number | null;
            source_block_idx?: number | null;
            compact_id?: string | null;
            reasoning_ms?: number | null;
            reasoning_tokens?: number | null;
            exec_ms?: number | null;
            show_timestamp: boolean;
            images?: string[] | null;
            image_captions?: string[] | null;
            sdk_calls?: components["schemas"]["SdkCall"][] | null;
            code_elapsed_ms?: number | null;
        };
        TimelineResponse: {
            items: components["schemas"]["TimelineItem"][];
            msg_count: number;
            has_more: boolean;
        };
        ToUserDocument: {
            content: string | null;
            version: string | null;
            modified_at: string | null;
            resources?: {
                [key: string]: components["schemas"]["ToUserResource"];
            };
            legacy_html: boolean;
        };
        ToUserResource: {
            id: string;
            name: string;
            markdown: boolean;
            version: string | null;
        };
        TokenUsageResponse: {
            input_tokens: number;
            output_tokens: number;
            reasoning_tokens: number;
            max_input_tokens: number;
            soft_compact_tokens: number;
            hard_compact_tokens: number;
        };
        TraceCheckpointMessagesResponse: {
            trace_id: string;
            agent_id: number;
            checkpoint_id: string | null;
            pruned: boolean;
            messages: {
                [key: string]: unknown;
            }[];
        };
        UiContributionsResponse: {
            themes: components["schemas"]["UiThemeContribution"][];
            nav: components["schemas"]["UiNavContribution"][];
            stats: components["schemas"]["UiStatContribution"][];
        };
        UiNavContribution: {
            plugin: string;
            location: string;
            label: string;
            icon: string;
            page: string;
        };
        UiStatContribution: {
            plugin: string;
            id: string;
            label: string;
        };
        UiThemeContribution: {
            plugin: string;
            name: string;
            tokens: {
                [key: string]: string;
            };
            dark_tokens?: {
                [key: string]: string;
            } | null;
        };
        UnitIdentity: {
            machine: string;
            home: string;
        };
        UploadedBatch: {
            files: components["schemas"]["UploadedFile"][];
        };
        UploadedFile: {
            filename: string;
            path: string;
            url: string;
            size: number;
            content_type: string;
        };
        UserSettingListResponse: {
            settings: components["schemas"]["UserSettingRow"][];
        };
        UserSettingRow: {
            key: string;
            value: unknown;
            updated_at: string;
        };
        UserSettingUpdateRequest: {
            value: unknown;
        };
        ValidationError: {
            loc: (string | number)[];
            msg: string;
            type: string;
            input?: unknown;
            ctx?: Record<string, never>;
        };
    };
    responses: never;
    parameters: never;
    requestBodies: never;
    headers: never;
    pathItems: never;
}
export type $defs = Record<string, never>;
export interface operations {
    login_api_auth_login_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LoginRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    issue_login_ticket_api_auth_login_ticket_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    redeem_login_ticket_api_auth_login_ticket_redeem_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LoginTicketRedeemRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    logout_api_auth_logout_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    check_api_auth_check_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    sessions_api_auth_sessions_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    }[];
                };
            };
        };
    };
    revoke_other_session_api_auth_sessions__session_id__revoke_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                session_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_bootstrap_api_bootstrap_get: {
        parameters: {
            query?: never;
            header?: {
                authorization?: string | null;
            };
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: string;
                    };
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_api_agents__agent_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentRow"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    patch_agent_api_agents__agent_id__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["LabelPatchRequest"];
            };
        };
        responses: {
            204: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_models_api_models_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ModelsResponse"];
                };
            };
        };
    };
    get_agents_api_agents_get: {
        parameters: {
            query?: {
                scope?: "live" | "terminated" | "all";
                query?: string;
                before_id?: number | null;
                limit?: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentDirectoryPage"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_roster_api_agents_roster_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentRoster"];
                };
            };
        };
    };
    post_guarded_agents_api_keyed_v1_agents_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SpawnAgentRequest"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SpawnedAgent"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_born_chain_api_agents__agent_id__born_chain_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BornChainResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_to_user_api_agents__agent_id__to_user_get: {
        parameters: {
            query?: {
                reference?: string[] | null;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ToUserDocument"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_resource_api_agents__agent_id__to_user_resource_get: {
        parameters: {
            query: {
                reference: string[];
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    retry_launch_api_keyed_v1_agents__agent_id__retry_launch_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["RetryLaunchRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RetryLaunchAccepted"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_force_expire_impersonation_api_agents__agent_id__impersonation_force_expire_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ForceExpireImpersonationRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ForceExpireImpersonationResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_agent_terminate_api_agents__agent_id__terminate_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": components["schemas"]["TerminateAgentRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TerminateAgentResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_agent_resurrect_api_agents__agent_id__resurrect_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": components["schemas"]["ResurrectAgentRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ResurrectAgentResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_agent_restart_api_agents__agent_id__restart_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: {
            content: {
                "application/json": components["schemas"]["RestartAgentRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RestartAgentResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    native_work_api_keyed_v1_agents__agent_id__native_work_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NativeWorkTarget"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    native_cancel_api_keyed_v1_agents__agent_id__cancel_work_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["NativeWorkTarget"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NativeCancelAcceptance"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    native_restart_api_keyed_v1_agents__agent_id__restart_work_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["NativeRestartRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NativeRestartAcceptance"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    native_restart_status_api_keyed_v1_agents__agent_id__restart_commands__command_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
                command_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NativeRestartProgress"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    guarded_compact_target_api_keyed_v1_agents__agent_id__compact_target_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CompactTarget"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    guarded_compact_history_api_keyed_v1_agents__agent_id__compact_history_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CompactTarget"];
            };
        };
        responses: {
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CompactAcceptance"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    guarded_compact_command_api_keyed_v1_agents__agent_id__compact_commands__command_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
                command_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CompactStatus"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_sessions_api_agents__agent_id__sessions_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SessionsResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_understanding_build_api_agents__agent_id__understanding_build_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["BuildRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BuildResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_understanding_build_api_agents__agent_id__understanding_builds__build_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
                build_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["BuildProgressResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_messages_api_agents__agent_id__messages_get: {
        parameters: {
            query?: {
                limit?: number | null;
                before?: number | null;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentMessagesResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_agent_message_api_agents__agent_id__messages_post: {
        parameters: {
            query?: never;
            header?: {
                "Idempotency-Key"?: string | null;
            };
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AgentMessageIn"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentMessageEnqueued"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_agent_system_note_api_agents__agent_id__system_note_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SystemNoteIn"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentMessageEnqueued"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reconcile_agent_message_api_agents__agent_id__messages_reconcile_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AgentMessageIn"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentMessageEnqueued"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_trace_checkpoint_messages_api_agents__agent_id__traces__trace_id__messages_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
                trace_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TraceCheckpointMessagesResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_last_message_api_agents__agent_id__last_message_get: {
        parameters: {
            query: {
                caller: string;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["LastMessageResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_pending_messages_api_agents__agent_id__pending_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PendingInbound"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_token_usage_api_agents__agent_id__token_usage_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TokenUsageResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_context_breakdown_api_agents__agent_id__context_breakdown_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ContextBreakdownResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_events_stream_api_agents__agent_id__events_stream_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_events_api_agents__agent_id__events_get: {
        parameters: {
            query?: {
                from?: string | null;
                to?: string | null;
                event?: string | null;
                level?: string | null;
                limit?: number | null;
                offset?: number;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentEventRow"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_computer_trace_api_computer_traces_get: {
        parameters: {
            query: {
                task_id: number;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_inspect_live_api_agents__agent_id__inspect_live_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentInspectLive"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_inspect_statistics_api_agents__agent_id__inspect_statistics_get: {
        parameters: {
            query?: {
                hours?: components["schemas"]["StatsWindowHours"] | null;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentInspectStatistics"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_neighbors_api_agents__agent_id__neighbors_get: {
        parameters: {
            query?: {
                depth?: number | null;
                limit?: number | null;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NeighborsResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_plugin_metrics_api_agents__agent_id__inspect_metrics_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PluginMetricResult"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_inspect_widgets_api_agents__agent_id__inspect_widgets_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["InspectWidgetResult"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_agent_shell_api_agents__agent_id__shell__session_id__get: {
        parameters: {
            query?: {
                lines?: number | null;
            };
            header?: never;
            path: {
                agent_id: number;
                session_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ShellCaptureResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_timeline_api_agents__agent_id__timeline_get: {
        parameters: {
            query?: {
                limit?: number | null;
                before?: string | null;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TimelineResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_retained_timeline_api_agents__agent_id__timeline_retained_get: {
        parameters: {
            query?: {
                limit?: number | null;
                checkpoint_id?: string | null;
                before?: string | null;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RetainedTimelineResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_reply_suggestion_api_agents__agent_id__reply_suggestion_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ReplySuggestionResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_system_broadcast_api_system_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    get_system_events_api_agents__agent_id__system_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_all_events_api_system_all_get: {
        parameters: {
            query?: {
                agents?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_host_permissions_api_host_permissions_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HostPermissionsPayload"];
                };
            };
        };
    };
    recheck_host_permissions_api_host_permissions_recheck_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HostPermissionsPayload"];
                };
            };
        };
    };
    request_host_permissions_api_host_permissions_request_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HostPermissionsPayload"];
                };
            };
        };
    };
    defer_host_permissions_api_host_permissions_defer_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HostPermissionsPayload"];
                };
            };
        };
    };
    post_guarded_page_register_api_keyed_v1_agents__agent_id__pages_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PageRegisterRequest"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PageRow"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_guarded_page_close_api_keyed_v1_agents__agent_id__pages__name__close_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path: {
                agent_id: number;
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PageCloseRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PageRow"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_all_pages_api_pages_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PageRow"][];
                };
            };
        };
    };
    get_pages_api_agents__agent_id__pages_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PageRow"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    proxy_page_root_new_pages__page_key__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                page_key: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    proxy_page_get_new_pages__page_key___rest__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                page_key: string;
                rest: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_open_notices_api_notices_open_get: {
        parameters: {
            query?: {
                limit?: number | null;
                include_awaiting?: boolean;
                agent_id?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NoticeItem"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_notices_live_api_notices_live_get: {
        parameters: {
            query?: {
                after?: number;
                limit?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NoticeItem"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_notices_feed_api_notices_get: {
        parameters: {
            query?: {
                limit?: number | null;
                resolved_limit?: number | null;
                before_at?: string | null;
                before_id?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NoticesFeed"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_notice_create_api_agents__agent_id__notices_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["NoticeCreateIn"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: unknown;
                    };
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    resolve_observed_api_keyed_v1_agents__agent_id__notices__notice_id__resolve_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path: {
                agent_id: number;
                notice_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ResolveNoticeIn"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentMessageEnqueued"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    edit_current_api_agents__agent_id__notices_current_guarded_v1_patch: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["GuardedNoticeEdit"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NoticeItem"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    dismiss_current_api_agents__agent_id__notices_current_dismiss_guarded_v1_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ObservedNotice"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["NoticeItem"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_cluster_stopping_api_cluster_stopping_post: {
        parameters: {
            query: {
                machine: string;
                home: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: string;
                    };
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_cluster_status_api_cluster_status_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ClusterStatus"];
                };
            };
        };
    };
    get_cluster_roster_api_cluster_roster_get: {
        parameters: {
            query?: {
                fresh?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MachineStatus"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_cluster_admin_events_api_cluster_admin_events_get: {
        parameters: {
            query?: {
                agent_id?: number | null;
                service_only?: boolean;
                level?: string | null;
                since?: string | null;
                event?: string | null;
                grep?: string | null;
                limit?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentEventsResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_cluster_machines_api_cluster_machines_get: {
        parameters: {
            query?: {
                fresh?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentMachineRow"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    set_machine_staging_api_cluster_machines__name__staging_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MachineStagingRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MachineDeleteResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_cluster_machine_api_cluster_machines__name__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MachineDeleteResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    pause_cluster_machine_api_cluster_machines__name__pause_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MachinePauseRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MachinePauseResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    resume_cluster_machine_api_cluster_machines__name__resume_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                name: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MachineResumeResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_commands_api_commands_get: {
        parameters: {
            query?: {
                agent_id?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CommandItem"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_config_api_config_get: {
        parameters: {
            query?: {
                machine?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConfigView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    put_config_api_config_put: {
        parameters: {
            query?: {
                machine?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": {
                    [key: string]: unknown;
                };
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConfigWriteResult"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_config_audit_api_config_audit_get: {
        parameters: {
            query?: {
                machine?: string | null;
                last?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConfigAuditView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_resolved_config_api_config_resolved_get: {
        parameters: {
            query?: {
                model?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ResolvedConfigView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_conversation_snapshot_api_agents__agent_id__conversation_snapshot_get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ConversationSnapshotResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_default_model_api_config_default_model_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DefaultModelView"];
                };
            };
        };
    };
    put_default_model_api_config_default_model_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["DefaultModelWrite"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DefaultModelView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_settings_api_settings_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UserSettingListResponse"];
                };
            };
        };
    };
    put_setting_api_settings__key__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                key: string;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["UserSettingUpdateRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UserSettingRow"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_schedules_api_schedules_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleSummary"][];
                };
            };
        };
    };
    create_schedule_api_schedules_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ScheduleCreate"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_schedule_api_schedules__schedule_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                schedule_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    update_schedule_api_schedules__schedule_id__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                schedule_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ScheduleUpdate"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    delete_schedule_api_schedules__schedule_id__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                schedule_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": {
                        [key: string]: string;
                    };
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    start_schedule_api_schedules__schedule_id__start_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                schedule_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    stop_schedule_api_schedules__schedule_id__stop_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                schedule_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    restart_schedule_api_schedules__schedule_id__restart_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                schedule_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_schedule_logs_api_schedules__schedule_id__logs_get: {
        parameters: {
            query?: {
                lines?: number;
            };
            header?: never;
            path: {
                schedule_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleLogsView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_schedule_runs_api_schedules__schedule_id__runs_get: {
        parameters: {
            query?: {
                limit?: number | null;
            };
            header?: never;
            path: {
                schedule_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleRunView"][];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    guarded_draft_schedule_api_keyed_v1_schedules_draft_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["ScheduleDraftRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["ScheduleDraftResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    guarded_draft_guide_api_keyed_v1_guide_draft_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["GuideDraftRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["GuideDraftResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_inventory_api_inventory_get: {
        parameters: {
            query?: {
                machine?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["InventoryAggregate"] | components["schemas"]["InventoryMachineView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    put_inventory_api_inventory_put: {
        parameters: {
            query?: {
                machine?: string | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["InventoryWriteRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["InventoryWriteResult"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_skills_api_skills_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SkillsView"];
                };
            };
        };
    };
    update_skill_enabled_api_skills_put: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["SkillEnableUpdate"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SkillView"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    guarded_draft_package_api_keyed_v1_packages_draft_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["PackageDraftRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["PackageDraftResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_metrics_api_metrics_get: {
        parameters: {
            query?: {
                days?: number | null;
                agent?: number | null;
                since_compact?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MetricsReport"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_metrics_agents_api_metrics_agents_get: {
        parameters: {
            query?: {
                days?: number | null;
                since_compact?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AgentMetricsReport"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_events_api_events_get: {
        parameters: {
            query?: {
                category?: string | null;
                event_name?: string | null;
                agent_id?: number | null;
                trace_id?: string | null;
                impersonation_session?: string | null;
                machine?: string | null;
                level?: string | null;
                tier?: string | null;
                from?: string | null;
                to?: string | null;
                hours?: number | null;
                limit?: number | null;
                offset?: number;
                with_total?: boolean;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EventsResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_run_timeline_messages_api_agents__agent_id__run_timeline_messages_get: {
        parameters: {
            query: {
                start: number;
                end: number;
                limit?: number;
                full?: boolean;
                snapshot?: string | null;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunTimelineMessages"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_run_timeline_context_api_agents__agent_id__run_timeline_context_get: {
        parameters: {
            query: {
                at: number;
                snapshot?: string | null;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunTimelineContext"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_run_timeline_api_agents__agent_id__run_timeline_get: {
        parameters: {
            query?: {
                from?: string | null;
                to?: string | null;
                recent_hours?: number | null;
                max_units?: number | null;
                snapshot?: string | null;
            };
            header?: never;
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunTimelineResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_run_timeline_links_api_insights_run_timeline_links_get: {
        parameters: {
            query: {
                agents: string;
                from: string;
                to: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunTimelineLinks"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_run_timeline_link_content_api_insights_run_timeline_link_content_get: {
        parameters: {
            query?: {
                inbound_id?: number | null;
                notice_id?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["RunTimelineLinkContent"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_event_resolutions_api_event_resolutions_get: {
        parameters: {
            query?: {
                status?: ("dismissed" | "reopened") | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EventResolutionListResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    create_event_resolution_api_event_resolutions_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["EventResolutionCreate"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EventResolutionRow"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    reopen_event_resolution_api_event_resolutions__dismissal_id__reopen_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                dismissal_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["EventResolutionRow"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_ops_monitor_api_ops_monitor_get: {
        parameters: {
            query?: {
                window?: "1h" | "6h" | "24h" | "7d";
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["OpsMonitorReport"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    list_alerts_api_alerts_get: {
        parameters: {
            query?: {
                window?: string;
                status?: ("unresolved" | "resolved") | null;
                severity?: ("critical" | "warning" | "error") | null;
                limit?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AlertsListResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    ingest_alerts_api_alerts_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["AlertWebhookPayload"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AlertIngestResult"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_alerts_stream_api_alerts_stream_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    get_health_api_health_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    get_stats_dashboard_api_stats_dashboard_get: {
        parameters: {
            query?: {
                hours?: components["schemas"]["StatsWindowHours"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["StatsDashboard"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_system_status_api_status_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["SystemStatus"];
                };
            };
        };
    };
    get_alert_classes_api_stats_alert_classes_get: {
        parameters: {
            query?: {
                hours?: components["schemas"]["StatsWindowHours"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AlertClassesResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_alert_class_samples_api_stats_alert_classes_samples_get: {
        parameters: {
            query: {
                level: "warning" | "error" | "critical";
                event_name: string;
                source: string;
                process?: string;
                hours?: components["schemas"]["StatsWindowHours"];
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["AlertClassSamples"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_memory_search_api_memory_search_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["MemorySearchRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MemorySearchResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_memory_refresh_api_memory_refresh_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MemoryRefreshResponse"];
                };
            };
        };
    };
    get_memory_graph_api_memory_graph_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MemoryGraphResponse"];
                };
            };
        };
    };
    get_memory_note_api_memory_note_get: {
        parameters: {
            query: {
                path: string;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["MemoryNoteResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_memory_pool_api_memory_pool_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content?: never;
            };
        };
    };
    get_mcp_clients_api_mcp_clients_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["McpClientView"][];
                };
            };
        };
    };
    post_mcp_client_revoke_api_mcp_clients__client_id__revoke_post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                client_id: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["McpClientRevoked"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_guarded_mcp_client_api_keyed_v1_mcp_clients_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["CredentialCreationRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["CredentialCreationResult"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_fleet_graph_api_fleet_graph_get: {
        parameters: {
            query?: {
                include_terminated?: boolean;
                hours?: components["schemas"]["StatsWindowHours"] | null;
                decay_lambda?: number | null;
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["FleetGraphResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    ingest_api_frontend_telemetry_post: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    grafana_root_grafana_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    grafana_proxy_get_grafana__rest__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                rest: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    grafana_proxy_put_grafana__rest__put: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                rest: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    grafana_proxy_post_grafana__rest__post: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                rest: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    grafana_proxy_delete_grafana__rest__delete: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                rest: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    grafana_proxy_head_grafana__rest__head: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                rest: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    grafana_proxy_patch_grafana__rest__patch: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                rest: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_okf_graph_api_okf_graph_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
        };
    };
    get_tasks_api_tasks_get: {
        parameters: {
            query?: {
                window?: string;
                fields?: "full" | "summary";
            };
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TaskListResponse"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    patch_task_api_tasks__task_id__patch: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
            };
            path: {
                task_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TaskUpdateRequest"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TaskRow"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    post_task_assignment_api_keyed_v1_task_assignments_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path?: never;
            cookie?: never;
        };
        requestBody: {
            content: {
                "application/json": components["schemas"]["TaskAssignmentIn"];
            };
        };
        responses: {
            201: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["TaskAssignmentAccepted"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    plugin_ui_root_api_plugin_ui__plugin__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                plugin: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    plugin_ui_get_api_plugin_ui__plugin___rest__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                plugin: string;
                rest: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    get_ui_contributions_api_ui_contributions_get: {
        parameters: {
            query?: never;
            header?: never;
            path?: never;
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UiContributionsResponse"];
                };
            };
        };
    };
    upload_files_api_agents__agent_id__uploads_post: {
        parameters: {
            query?: {
                deliver?: boolean;
            };
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "multipart/form-data": components["schemas"]["Body_upload_files_api_agents__agent_id__uploads_post"];
            };
        };
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["UploadedBatch"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    serve_upload_api_agents__agent_id__uploads__filename__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
                filename: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    upload_api_keyed_v1_agents__agent_id__uploads_post: {
        parameters: {
            query?: never;
            header: {
                "Idempotency-Key": string;
                "Idempotency-Scope": string;
            };
            path: {
                agent_id: number;
            };
            cookie?: never;
        };
        requestBody: {
            content: {
                "multipart/form-data": components["schemas"]["Body_upload_api_keyed_v1_agents__agent_id__uploads_post"];
            };
        };
        responses: {
            202: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["Acceptance"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    status_api_keyed_v1_agents__agent_id__uploads__batch_id__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
                batch_id: string;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["DeliveryStatus"];
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
    object_file_api_keyed_v1_agents__agent_id__uploads__batch_id__objects__ordinal__get: {
        parameters: {
            query?: never;
            header?: never;
            path: {
                agent_id: number;
                batch_id: string;
                ordinal: number;
            };
            cookie?: never;
        };
        requestBody?: never;
        responses: {
            200: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": unknown;
                };
            };
            422: {
                headers: {
                    [name: string]: unknown;
                };
                content: {
                    "application/json": components["schemas"]["HTTPValidationError"];
                };
            };
        };
    };
}
