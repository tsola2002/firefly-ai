SELECT pair, highest_green_streak, highest_red_streak
FROM pair_reports
ORDER BY highest_red_streak DESC;


SELECT *
FROM streak_frequencies
WHERE color = 'RED'
AND streak_length >= 10;