namespace SportsHedge.Launcher;

/// <summary>Small temporary progress window shown only while Sports Hedge starts.</summary>
internal sealed class StartupWindow : Form
{
    private readonly Label _status;

    public StartupWindow()
    {
        Text = "Sports Hedge — PAPER MODE";
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        MinimizeBox = true;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(420, 132);
        ShowInTaskbar = true;
        Icon = TrayIconFactory.Create();

        var title = new Label
        {
            Text = "Sports Hedge · PAPER MODE · no live execution",
            Font = new Font(Font, FontStyle.Bold),
            AutoSize = false,
            Location = new Point(16, 14),
            Size = new Size(388, 22),
        };
        _status = new Label
        {
            Text = "Starting Sports Hedge…",
            AutoSize = false,
            Location = new Point(16, 44),
            Size = new Size(388, 22),
        };
        var progress = new ProgressBar
        {
            Style = ProgressBarStyle.Marquee,
            MarqueeAnimationSpeed = 30,
            Location = new Point(16, 74),
            Size = new Size(388, 16),
        };
        var hint = new Label
        {
            Text = "The interface opens in your browser when ready. Building the interface can take a minute.",
            ForeColor = SystemColors.GrayText,
            AutoSize = false,
            Location = new Point(16, 98),
            Size = new Size(388, 30),
        };
        Controls.AddRange(new Control[] { title, _status, progress, hint });
    }

    public void SetStatus(string message)
    {
        _status.Text = message;
    }
}

internal static class TrayIconFactory
{
    /// <summary>Draws the "SH" mark at runtime so no binary icon asset is committed.</summary>
    public static Icon Create()
    {
        using var bitmap = new Bitmap(32, 32);
        using (var g = Graphics.FromImage(bitmap))
        {
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.AntiAliasGridFit;
            using var background = new SolidBrush(Color.FromArgb(17, 30, 27));
            using var border = new Pen(Color.FromArgb(124, 242, 176), 2);
            g.FillRectangle(background, 1, 1, 30, 30);
            g.DrawRectangle(border, 1, 1, 29, 29);
            using var font = new Font("Segoe UI", 11, FontStyle.Bold, GraphicsUnit.Pixel);
            using var text = new SolidBrush(Color.FromArgb(124, 242, 176));
            var size = g.MeasureString("SH", font);
            g.DrawString("SH", font, text, (32 - size.Width) / 2, (32 - size.Height) / 2);
        }
        var handle = bitmap.GetHicon();
        using var temporary = Icon.FromHandle(handle);
        var icon = (Icon)temporary.Clone();
        DestroyIcon(handle);
        return icon;
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    private static extern bool DestroyIcon(IntPtr handle);
}
