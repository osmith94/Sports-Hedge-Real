using Microsoft.Win32;
using SportsHedge.Launcher.Core;
using SportsHedge.Launcher.Core.Logging;
using SportsHedge.Launcher.Core.State;

namespace SportsHedge.Launcher;

/// <summary>
/// Resident tray presence. The browser is the primary UI; this only shows
/// state, reopens the browser and routes tray Exit into the controller's
/// single shutdown state machine.
/// </summary>
internal sealed class TrayApplicationContext : ApplicationContext
{
    private static readonly TimeSpan SessionEndingWait = TimeSpan.FromSeconds(20);
    private readonly SessionController _controller;
    private readonly ILog _log;
    private readonly SynchronizationContext _ui;
    private readonly NotifyIcon _tray;
    private readonly ToolStripMenuItem _openItem;
    private readonly ToolStripMenuItem _exitItem;
    private readonly StartupWindow _startupWindow;
    private readonly Icon _icon;
    private bool _exiting;

    public TrayApplicationContext(SessionController controller, WindowsInstanceActivation activation, ILog log)
    {
        _controller = controller;
        _log = log;
        _icon = TrayIconFactory.Create();

        _openItem = new ToolStripMenuItem("Open Sports Hedge", null, (_, _) => OpenBrowser()) { Enabled = false };
        _exitItem = new ToolStripMenuItem("Exit Sports Hedge", null, (_, _) => ConfirmAndExit(ShutdownSource.Tray));
        var menu = new ContextMenuStrip();
        menu.Items.Add(_openItem);
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(new ToolStripMenuItem("PAPER MODE — no live execution") { Enabled = false });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add(_exitItem);

        _tray = new NotifyIcon
        {
            Icon = _icon,
            Text = LauncherConstants.TrayStarting,
            ContextMenuStrip = menu,
            Visible = true,
        };
        _tray.DoubleClick += (_, _) => OpenBrowser();

        _startupWindow = new StartupWindow();
        _startupWindow.FormClosing += (_, e) =>
        {
            if (e.CloseReason == CloseReason.UserClosing && !_exiting)
            {
                // Closing the progress window only hides it; Sports Hedge keeps starting.
                e.Cancel = true;
                _startupWindow.Hide();
            }
        };
        _startupWindow.Show();
        _ui = SynchronizationContext.Current ?? new WindowsFormsSynchronizationContext();

        _controller.State.Changed += status => _ui.Post(_ => OnStatusChanged(status), null);
        _controller.Completion.ContinueWith(t => _ui.Post(_ => ExitApplication(t.IsCompletedSuccessfully ? t.Result : null), null), TaskScheduler.Default);
        activation.Listen(() => _ui.Post(_ => OnActivatedBySecondInstance(), null));
        SystemEvents.SessionEnding += OnSessionEnding;

        _ = StartAsync();
    }

    public int ExitCode { get; private set; }

    private async Task StartAsync()
    {
        var result = await Task.Run(_controller.StartAsync).ConfigureAwait(true);
        if (result.Success)
        {
            _startupWindow.Hide();
            OpenBrowser();
            _tray.ShowBalloonTip(4000, "Sports Hedge", "Sports Hedge is running in PAPER MODE. Closing the browser does not stop it; use Exit Sports Hedge.", ToolTipIcon.Info);
            return;
        }
        if (result.StoppedByRequest)
        {
            return;
        }
        ExitCode = 1;
        _startupWindow.Hide();
        _tray.Text = LauncherConstants.TrayFailed;
        MessageBox.Show(
            OperatorMessages.StartupFailure(result, _controller.Layout.LogsDir),
            "Sports Hedge could not start",
            MessageBoxButtons.OK,
            MessageBoxIcon.Error);
    }

    private void OnStatusChanged(ControllerStatus status)
    {
        _tray.Text = Truncate(OperatorMessages.TrayText(status.State));
        _openItem.Enabled = OperatorMessages.CanOpenBrowser(status.State);
        _exitItem.Enabled = status.State is not (ControllerState.Stopping or ControllerState.Stopped or ControllerState.Failed or ControllerState.ShutdownIncomplete);
        if (status.State == ControllerState.Starting)
        {
            _startupWindow.SetStatus(status.Message);
        }
        else if (status.State == ControllerState.Degraded)
        {
            _tray.ShowBalloonTip(10000, "Sports Hedge — Degraded", status.Message, ToolTipIcon.Warning);
        }
        else if (status.State == ControllerState.Stopping)
        {
            _startupWindow.Hide();
        }
    }

    private void OpenBrowser()
    {
        if (OperatorMessages.CanOpenBrowser(_controller.State.State))
        {
            BrowserLauncher.Open(LauncherConstants.AppUrl);
        }
    }

    private void OnActivatedBySecondInstance()
    {
        _log.Info("second_instance_activation");
        if (OperatorMessages.CanOpenBrowser(_controller.State.State))
        {
            OpenBrowser();
        }
        else if (_controller.State.State == ControllerState.Starting)
        {
            _startupWindow.Show();
            _startupWindow.Activate();
        }
    }

    private void ConfirmAndExit(ShutdownSource source)
    {
        var answer = MessageBox.Show(
            OperatorMessages.ExitConfirmBody,
            OperatorMessages.ExitConfirmTitle,
            MessageBoxButtons.OKCancel,
            MessageBoxIcon.Question,
            MessageBoxDefaultButton.Button2);
        if (answer == DialogResult.OK)
        {
            _controller.RequestShutdown(source);
        }
    }

    private void OnSessionEnding(object? sender, SessionEndingEventArgs e)
    {
        _log.Info($"windows_session_ending reason={e.Reason}");
        _controller.RequestShutdown(ShutdownSource.ControllerExit);
        _controller.Completion.Wait(SessionEndingWait);
    }

    public void OnUnhandledException(Exception exception)
    {
        _log.Error($"unhandled_ui_exception {exception}");
        _controller.RequestShutdown(ShutdownSource.ControllerExit);
    }

    private void ExitApplication(ShutdownReport? report)
    {
        if (_exiting)
        {
            return;
        }
        _exiting = true;
        if (report is { Failed: false, CleanStop: false })
        {
            ExitCode = 2;
            MessageBox.Show(
                $"{OperatorMessages.ShutdownOutcome(report)}\n\nLogs: {_controller.Layout.LogsDir}",
                "Sports Hedge — shutdown incomplete",
                MessageBoxButtons.OK,
                MessageBoxIcon.Warning);
        }
        SystemEvents.SessionEnding -= OnSessionEnding;
        _tray.Visible = false;
        _startupWindow.Close();
        ExitThread();
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            SystemEvents.SessionEnding -= OnSessionEnding;
            _tray.Dispose();
            _startupWindow.Dispose();
            _icon.Dispose();
        }
        base.Dispose(disposing);
    }

    private static string Truncate(string text) => text.Length <= 127 ? text : text[..127];
}
